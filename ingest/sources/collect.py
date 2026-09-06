"""集めたソース —— AI が集めたものを長期記憶へ焼く。

素材を持っているのは配信側(`app/collect.py`)で、こちらは**取りに行って焼くだけ**。
長期記憶(`corpus/`)へ書けるのは ingest だけ、という線をこの層でも崩さないための形で、
やり取りは固化(`sources/memory.py`)とまったく同じ契約(`sources/remote.py`)を使う。

固化と違うのは **1 つではなく可変**なところ。収集は画面や REST から実行時に増えるので、
アダプタを固定で並べられない。`ADAPTERS` に書く代わりに、**カタログを配信側に聞いて
その場で組み立てる**(`sources/__init__.py` の `collect_adapters`)。

**毎回焼き直すが、中身は積み上がる。** 素材が「前世代 + 新しく集めたぶん」なので、
ブルーグリーン(全件の作り直し)に乗せたまま追記として振る舞う —— 世代は今と 1 つ前
だけが残る(`main.switch_db`)。

**集めるのは `/fetch` の中**(取り込みの中で AI が動く)。**取ってきたものは
他のソースのダンプと同じように `dumps/` に置き、焼き上がってから消す** ——
途中で落ちても、次に走らせたときは AI を呼び直さずその場に残ったものから焼ける。
ダンプが二度と手に入らない類のデータなので、他のソースより取り直しの代償が大きい
(AI の 1 回ぶんが消える)。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from core import SourceAdapter
from sources.remote import PluginError, RemotePluginAdapter, RemoteSource, _get_json

log = logging.getLogger("chiezo.ingest")

# 素材を配る相手。compose ではサービス名で届くので、通常は書き換えない
APP_URL_ENV = "CHIEZO_APP_URL"
DEFAULT_APP_URL = "http://chiezo-app:7010"

# カタログを引くときの上限(秒)。配信側はローカルの SQLite を読むだけなので短くてよい
CATALOG_TIMEOUT = 10.0


def app_base_url() -> str:
    return (os.environ.get(APP_URL_ENV) or DEFAULT_APP_URL).strip().rstrip("/")


def base_url() -> str:
    return f"{app_base_url()}/v1/collect"


class CollectAdapter(RemotePluginAdapter):
    """`RemotePluginAdapter` に「取り直さない」だけを足したもの。

    素材は AI が集めたその 1 回ぶんで、取り直せば同じものは返ってこない
    (`{cursor}` が進んでいるので次の範囲になる)。焼く前に落ちたときのために
    `dumps/` に残ったものを拾い、**焼き上がってから消す**。
    """

    def __init__(self, src: RemoteSource) -> None:
        super().__init__(src)
        self._staged: Path | None = None

    def fetch(self, workdir: Path) -> tuple[Path, str]:
        if leftover := self._leftover(workdir):
            date = leftover.stem.rsplit("-", 1)[-1]
            log.info("reusing staged material %s (skipping the AI call)", leftover.name)
            self._staged = leftover
            self._apply_meta(leftover)
            return leftover, date
        path, date = super().fetch(workdir)
        self._staged = path
        return path, date

    def _leftover(self, workdir: Path) -> Path | None:
        """前回の取り込みが焼き切れずに残した素材。新しいものを採る。"""
        found = sorted(workdir.glob(f"{self.source}-*.ndjson"))
        return found[-1] if found else None

    def on_success(self, _final_path: Path) -> None:
        """焼けたので素材を捨てる。

        **残すと次回が古い素材を焼き直す** —— `_leftover` が拾ってしまい、
        AI が呼ばれないまま同じ中身が積み上がる。中身はもう DB に入っている。
        """
        if self._staged and self._staged.exists():
            log.info("removing staged material %s", self._staged.name)
            self._staged.unlink()
        self._staged = None


def _source(entry: dict) -> RemoteSource:
    return RemoteSource(
        base_url=base_url(),
        name=str(entry["name"]),
        kind=str(entry.get("kind") or "collect"),
        lang=entry.get("lang"),
        label=str(entry.get("label") or entry["name"]),
        min_docs=int(entry.get("min_docs") or 1),
        memory_gb=float(entry.get("memory_gb") or 0.5),
    )


def catalog() -> list[RemoteSource]:
    """いま焼ける収集の一覧を配信側に聞く。

    **繋がらなくても落とさない**(空で返す)。収集は任意の層で、配信側が
    立っていない・無効にしている構成が普通にある —— そこで ingest 全体を
    止めると、他のソースの取り込みまで巻き添えになる。
    """
    url = f"{base_url()}/sources"
    try:
        payload = _get_json(url, CATALOG_TIMEOUT)
    except Exception as e:
        log.debug("collect catalog unavailable (%s): %s", url, e)
        return []
    entries = payload.get("sources")
    if not isinstance(entries, list):
        raise PluginError(f"{url} must return a 'sources' list")
    return [_source(e) for e in entries if isinstance(e, dict) and e.get("name")]


def adapters() -> dict[str, callable]:
    """`ADAPTERS` に混ぜる形(名前 → 生成関数)。"""
    return {src.name: (lambda s=src: CollectAdapter(s)) for src in catalog()}


def adapter_for(name: str) -> SourceAdapter | None:
    """名前で 1 つだけ引く(`get_adapter` から呼ぶ)。"""
    for src in catalog():
        if src.name == name:
            return CollectAdapter(src)
    return None
