"""Wikipedia (CirrusSearch ダンプ) アダプタ。

wiki_id をパラメータ化しており、jawiki / enwiki / … で再利用できる(設計書 §7.1)。
データ形式: JSON Lines、2 行 1 組
  1 行目: {"index": {"_id": "12345", ...}}
  2 行目: ドキュメント本体(title, text, opening_text, category, ...)
"""
from __future__ import annotations

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

DUMP_INDEX_URL = "https://dumps.wikimedia.org/other/cirrussearch/current/"

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
        """current ディレクトリの一覧から最新のダンプ日付を得る。DUMP_DATE 環境変数で上書き可。"""
        if date := os.environ.get("DUMP_DATE"):
            return date
        with urllib.request.urlopen(DUMP_INDEX_URL, timeout=60) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        pattern = rf"{re.escape(self.source)}-(\d{{8}})-cirrussearch-content\.json\.gz"
        dates = sorted(set(re.findall(pattern, html)))
        if not dates:
            raise RuntimeError(f"no cirrussearch content dump found for {self.source}")
        return dates[-1]

    def fetch(self, workdir: Path) -> tuple[Path, str]:
        """ダンプを取得しローカルパスとダンプ日付を返す。curl -C - で再開可能。"""
        date = self.latest_dump_date()
        filename = f"{self.source}-{date}-cirrussearch-content.json.gz"
        dest = workdir / filename
        if dest.exists() and not (dest.with_suffix(".gz.part")).exists():
            log.info("dump already downloaded: %s", dest)
            return dest, date
        url = DUMP_INDEX_URL + filename
        part = dest.with_suffix(".gz.part")
        log.info("downloading %s", url)
        subprocess.run(
            ["curl", "-fSL", "--retry", "5", "-C", "-", "-o", str(part), url],
            check=True,
        )
        part.rename(dest)
        return dest, date

    # ---- 変換 -------------------------------------------------------------

    def iter_docs(self, path: Path) -> Iterator[Doc]:
        """CirrusSearch ダンプをストリーミングで読み、コアスキーマの Doc を返す。

        namespace != 0 の文書はスキップ。redirect は ns=0 のもののみ aliases に展開。
        """
        with gzip.open(path, "rt", encoding="utf-8") as f:
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
