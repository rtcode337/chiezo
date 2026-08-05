"""別コンテナで動くプラグインからソースを借りる(サイドカー方式)。

`CHIEZO_PLUGIN_SOURCES` に並べた URL へ問い合わせ、そのプラグインが提供するソースを
組み込みのアダプタと同じように扱えるようにする。

**役割の割り方が肝心**: 取得と整形はプラグイン、DB の構築(FTS・タグ転置表・世代
切り替え・検証)は本体。この向きにすると、

- **プラグインは Chiezo のコードを一切含まない**(言語も自由。イメージも小さい)
- **スキーマ版が上がってもプラグインを焼き直さなくてよい**(DB を焼くのは本体)
- プラグインに `/data` の書き込み権限が要らない

使い方は `docs/adding-a-source.md` のケース 3 が正。

契約は 2 つの口だけ:

    GET {base}/sources            → {"sources": [{"name","kind","lang",…}, …]}
    GET {base}/fetch?source=NAME  → NDJSON(1 行目に meta、以降は 1 行 1 文書)

`/fetch` の 1 行目は `meta` を持つオブジェクトにできる(省略可):

    {"meta": {"dump_date": "20260805", "min_docs": 20000, "sample_titles": ["…"]}}

- `dump_date` … 元データの日付。世代ファイル名 `<source>-<date>.db` になる
- `min_docs` / `sample_titles` … 検証条件。カタログの値を上書きする

**メタをヘッダではなく本文の 1 行目に置く**のは、HTTP ヘッダが latin-1 しか運べず
日本語のタイトルを載せられないため。取り込んだ中身を見てからでないと代表を選べない
ソース(固有名を焼き込まず、取り込んだ文書から検証用の代表を選ぶ類)は、
全部数え終えてから 1 行目を書き出せばよい。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from core import Doc, SourceAdapter

log = logging.getLogger("chiezo.ingest")

PLUGIN_ENV = "CHIEZO_PLUGIN_SOURCES"

# カタログ取得の待ち時間。プラグインが落ちている・起動途中でも本体を止めないよう短くする。
CATALOG_TIMEOUT = float(os.environ.get("CHIEZO_PLUGIN_TIMEOUT") or 5.0)

# ソース名に許す文字。組み込みと同じ制限(`-` は世代ファイル名 `<source>-<date>.db` の
# 区切りと衝突する)。
_SOURCE_NAME = re.compile(r"^[A-Za-z0-9_]+$")


class PluginError(Exception):
    """プラグインが契約を満たしていない(応答の形がおかしい)。"""


@dataclass
class RemoteSource:
    """プラグインのカタログ 1 行。管理画面の表示にもそのまま使う。"""

    base_url: str
    name: str
    kind: str
    lang: str | None = None
    label: str | None = None
    min_docs: int = 1
    sample_titles: list[str] = field(default_factory=list)
    memory_gb: float = 0.5


def _base_urls(spec: str | None = None) -> list[str]:
    raw = os.environ.get(PLUGIN_ENV, "") if spec is None else spec
    return [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]


def _get_json(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def _parse_catalog(base: str, payload: dict) -> list[RemoteSource]:
    entries = payload.get("sources")
    if not isinstance(entries, list) or not entries:
        raise PluginError(f"{base}/sources must return a non-empty 'sources' list")
    out: list[RemoteSource] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise PluginError(f"{base}/sources: each entry must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not _SOURCE_NAME.match(name):
            raise PluginError(
                f"{base}/sources: invalid source name {name!r}"
                " (use [A-Za-z0-9_] only; '-' collides with the <source>-<date>.db separator)"
            )
        kind = entry.get("kind")
        if not isinstance(kind, str) or not kind:
            raise PluginError(f"{base}/sources: {name!r} must have a 'kind'")
        out.append(RemoteSource(
            base_url=base,
            name=name,
            kind=kind,
            lang=entry.get("lang") or None,
            label=entry.get("label") or None,
            min_docs=max(1, int(entry.get("min_docs") or 1)),
            sample_titles=[str(t) for t in (entry.get("sample_titles") or [])],
            memory_gb=float(entry.get("memory_gb") or 0.5),
        ))
    return out


def catalog(spec: str | None = None) -> list[RemoteSource]:
    """設定されたプラグインすべてのカタログ。到達できないものは飛ばす。

    **到達不能は警告にとどめ、応答の形の誤りは落とす。** 相手は別コンテナなので、
    再起動中や起動順の都合で一時的に繋がらないのは正常な状態でありうる —— そこで
    本体を止めると、プラグインが 1 つ死んだだけで Chiezo 全体が動かなくなる。
    一方、繋がったのに形が違うのは直すべき不具合なので黙って無視しない。
    """
    found: dict[str, RemoteSource] = {}
    for base in _base_urls(spec):
        try:
            payload = _get_json(f"{base}/sources", CATALOG_TIMEOUT)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
            log.warning("plugin %s: catalog unavailable (%s)", base, e)
            continue
        for src in _parse_catalog(base, payload):
            if src.name in found:
                raise PluginError(
                    f"{base}/sources: source {src.name!r} is already provided by"
                    f" {found[src.name].base_url}; rename it instead of shadowing"
                )
            found[src.name] = src
    return list(found.values())


class RemotePluginAdapter:
    """プラグインが配る NDJSON を読んで `Doc` にするアダプタ。

    組み込みのアダプタと同じ `SourceAdapter` の形に収まるので、構築・検証・世代切り替えの
    仕掛けは本体のものがそのまま効く。
    """

    def __init__(self, src: RemoteSource) -> None:
        self.src = src
        self.source = src.name
        self.source_kind = src.kind
        self.lang = src.lang
        self.min_docs = src.min_docs
        self.sample_titles = list(src.sample_titles)
        # 構築するのは本体側で、持つのは NDJSON 1 行ぶんだけ。ストリームで書き出すので
        # 文書数に関わらずメモリは増えない
        self.min_build_memory_gb = src.memory_gb

    def fetch(self, workdir: Path) -> tuple[Path, str]:
        """`/fetch` の中身をそのままファイルへ落とす(再開はしない)。

        ダウンロードを分けているのは組み込みのアダプタと同じ理由で、取得と解析を
        分けておくと `DUMP_FILE` で手元のファイルから作り直せるため。
        """
        url = f"{self.src.base_url}/fetch?" + urllib.parse.urlencode({"source": self.source})
        log.info("fetching %s from %s", self.source, self.src.base_url)
        # 日付は本文の 1 行目にあるので、いったん仮の名前で落としてから付け直す
        staging = workdir / f"{self.source}.ndjson.part"
        with urllib.request.urlopen(url, timeout=None) as res, staging.open("wb") as f:
            shutil.copyfileobj(res, f)
        dump_date = self._apply_meta(staging)
        path = workdir / f"{self.source}-{dump_date}.ndjson"
        staging.replace(path)
        log.info("fetched %s (%.1f MiB)", path.name, path.stat().st_size / 1024 / 1024)
        return path, dump_date

    def _apply_meta(self, path: Path) -> str:
        """1 行目の `meta` を読み、検証条件を上書きしてダンプ日付を返す。

        meta が無ければ取り込んだ日を日付にする(常に最新しか配らないプラグイン向け)。
        """
        with path.open(encoding="utf-8") as f:
            first = f.readline().strip()
        meta = {}
        if first:
            try:
                raw = json.loads(first)
            except ValueError:
                raw = {}
            if isinstance(raw, dict) and isinstance(raw.get("meta"), dict):
                meta = raw["meta"]
        if isinstance(titles := meta.get("sample_titles"), list) and titles:
            self.sample_titles = [str(t) for t in titles]
        if (min_docs := meta.get("min_docs")) is not None:
            try:
                self.min_docs = max(1, int(min_docs))
            except (TypeError, ValueError):
                log.warning("plugin %s: ignoring invalid min_docs=%r", self.source, min_docs)
        dump_date = str(meta.get("dump_date") or "").strip()
        return dump_date if re.fullmatch(r"\d{8}", dump_date) else datetime.now(UTC).strftime("%Y%m%d")

    def iter_docs(self, path: Path) -> Iterator[Doc]:
        """NDJSON を 1 行ずつ `Doc` にする。

        **`doc_id` は省略できる**(その場合は 1 から順に振る)。元データ側に安定した
        id があるなら渡してもらうほうがよい —— 取り込み直しても同じ id になり、
        文書 URL(`/search/<source>/doc/<id>`)が変わらない。
        """
        with path.open(encoding="utf-8") as f:
            for n, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except ValueError as e:
                    raise PluginError(f"{self.source}: line {n} is not valid JSON: {e}") from None
                # 1 行目の meta は文書ではない(fetch() が読んで検証条件に使う)
                if n == 1 and isinstance(raw, dict) and isinstance(raw.get("meta"), dict):
                    continue
                if not isinstance(raw, dict) or not str(raw.get("title") or "").strip():
                    raise PluginError(f"{self.source}: line {n} must be an object with a title")
                yield Doc(
                    doc_id=int(raw.get("doc_id") or n),
                    title=str(raw["title"]),
                    opening=raw.get("opening"),
                    body=raw.get("body"),
                    tags=[str(t) for t in (raw.get("tags") or [])],
                    links=[str(t) for t in (raw.get("links") or [])],
                    aliases=[str(t) for t in (raw.get("aliases") or [])],
                    updated_at=raw.get("updated_at"),
                    rank_score=float(raw.get("rank_score") or 0.0),
                    extra=raw.get("extra") if isinstance(raw.get("extra"), dict) else None,
                )


def load_remote_adapters(spec: str | None = None) -> dict[str, Callable[[], SourceAdapter]]:
    """プラグインのソース名 → アダプタ生成関数。

    **問い合わせるのは呼ばれたときだけ**(import 時ではない)。別コンテナが起動して
    いない段階で本体の import が失敗すると、起動順に依存する脆い構成になるため。
    """
    return {src.name: (lambda s=src: RemotePluginAdapter(s)) for src in catalog(spec)}
