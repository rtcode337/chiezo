"""Wikipedia (CirrusSearch ダンプ) アダプタ。

wiki_id をパラメータ化しており、jawiki / enwiki / … で再利用できる(設計書 §7.1)。
データ形式: JSON Lines、2 行 1 組
  1 行目: {"index": {"_id": "12345", ...}}
  2 行目: ドキュメント本体(title, text, opening_text, category, ...)

旧 other/cirrussearch/ (単一 .json.gz) は 2026 年に廃止され、
other/cirrus_search_index/<date>/index_name=<wiki_id>_content/ 配下の
複数 .json.bz2 シャードに置き換わった(DEPRECATED.txt 参照)。
"""
from __future__ import annotations

import bz2
import gzip
import json
import logging
import os
import re
import subprocess
import urllib.request
from pathlib import Path
from typing import Iterator

from core import Doc

log = logging.getLogger(__name__)

DUMP_INDEX_URL = "https://dumps.wikimedia.org/other/cirrus_search_index/"

# Wikimedia は User-Agent の無い/汎用スクリプト由来のリクエストを 403 で拒否するため、
# 連絡先付きの UA を明示する(https://meta.wikimedia.org/wiki/User-Agent_policy)。
USER_AGENT = "chiezo-ingest/0.1 (https://github.com/; contact via repo issues)"


def _http_get(url: str, timeout: int = 60) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")

DEFAULT_VALIDATION = {
    # 検証パラメータ(設計書 §6.1-4)。フィクスチャテストではコンストラクタで上書きする。
    "jawiki": {
        "min_docs": 1_000_000,
        "sample_titles": [
            "東京都", "浅草寺", "関ヶ原の戦い", "富士山", "夏目漱石",
            "日本", "京都市", "新幹線", "源氏物語", "大阪府",
        ],
    },
    "enwiki": {
        "min_docs": 5_000_000,
        "sample_titles": [
            "Tokyo", "United States", "Albert Einstein", "Python (programming language)",
            "World War II", "London", "Mathematics", "William Shakespeare",
            "Mount Everest", "Internet",
        ],
    },
}


class WikipediaAdapter:
    source_kind = "wikipedia"

    def __init__(
        self,
        wiki_id: str,
        lang: str,
        min_docs: int | None = None,
        sample_titles: list[str] | None = None,
    ):
        self.source = wiki_id
        self.lang = lang
        defaults = DEFAULT_VALIDATION.get(wiki_id, {})
        self.min_docs = min_docs if min_docs is not None else defaults.get("min_docs", 1)
        self.sample_titles = (
            sample_titles if sample_titles is not None else defaults.get("sample_titles", [])
        )

    # ---- 取得 -------------------------------------------------------------

    def latest_dump_date(self) -> str:
        """親ディレクトリの一覧から最新のダンプ日付を得る。DUMP_DATE 環境変数で上書き可。"""
        if date := os.environ.get("DUMP_DATE"):
            return date
        html = _http_get(DUMP_INDEX_URL)
        dates = sorted(set(re.findall(r'href="(\d{8})/"', html)))
        if not dates:
            raise RuntimeError("no cirrus_search_index date directories found")
        return dates[-1]

    def _shard_filenames(self, date: str) -> list[str]:
        """指定日付ディレクトリ配下の index_name=<source>_content シャード一覧。"""
        dir_url = f"{DUMP_INDEX_URL}{date}/index_name={self.source}_content/"
        html = _http_get(dir_url)
        pattern = rf'href="({re.escape(self.source)}_content-{date}-\d+\.json\.bz2)"'
        names = sorted(set(re.findall(pattern, html)))
        if not names:
            raise RuntimeError(f"no cirrus_search_index shards found for {self.source} @ {date}")
        return names

    def fetch(self, workdir: Path) -> tuple[list[Path], str]:
        """全シャードを取得しローカルパス一覧とダンプ日付を返す。curl -C - で再開可能。"""
        date = self.latest_dump_date()
        dir_url = f"{DUMP_INDEX_URL}{date}/index_name={self.source}_content/"
        paths = []
        for filename in self._shard_filenames(date):
            dest = workdir / filename
            part = dest.with_suffix(".bz2.part")
            if dest.exists() and not part.exists():
                log.info("shard already downloaded: %s", dest)
                paths.append(dest)
                continue
            url = dir_url + filename
            log.info("downloading %s", url)
            subprocess.run(
                ["curl", "-fSL", "-A", USER_AGENT, "--retry", "5", "-C", "-", "-o", str(part), url],
                check=True,
            )
            part.rename(dest)
            paths.append(dest)
        return paths, date

    # ---- 変換 -------------------------------------------------------------

    def iter_docs(self, path: Path | list[Path]) -> Iterator[Doc]:
        """CirrusSearch ダンプをストリーミングで読み、コアスキーマの Doc を返す。

        namespace != 0 の文書はスキップ。redirect は ns=0 のもののみ aliases に展開。
        単一ファイル(.gz、テスト用フィクスチャ)と複数シャード(.bz2、本番)の両方に対応。
        """
        paths = path if isinstance(path, list) else [path]
        for p in paths:
            yield from self._iter_docs_one(p)

    def _iter_docs_one(self, path: Path) -> Iterator[Doc]:
        opener = bz2.open if path.suffix == ".bz2" else gzip.open
        with opener(path, "rt", encoding="utf-8") as f:
            while True:
                index_line = f.readline()
                if not index_line:
                    break
                doc_line = f.readline()
                if not doc_line:
                    break
                index_line = index_line.strip()
                doc_line = doc_line.strip()
                if not index_line or not doc_line:
                    continue
                header = json.loads(index_line)
                raw = json.loads(doc_line)
                if raw.get("namespace", 0) != 0:
                    continue
                doc_id = int(header["index"]["_id"])
                aliases = [
                    r["title"]
                    for r in raw.get("redirect") or []
                    if r.get("namespace", 0) == 0 and r.get("title")
                ]
                yield Doc(
                    doc_id=doc_id,
                    title=raw["title"],
                    opening=raw.get("opening_text"),
                    body=raw.get("text"),
                    tags=raw.get("category") or [],
                    links=raw.get("outgoing_link") or [],
                    aliases=aliases,
                    updated_at=raw.get("timestamp"),
                    rank_score=float(raw.get("popularity_score") or 0.0),
                    extra=None,
                )
