"""Wikipedia (標準 XML ダンプ) アダプタ。

wiki_id をパラメータ化しており、jawiki / enwiki / … で再利用できる(設計書 §7.1)。

旧実装は CirrusSearch ダンプ(JSON Lines)の "text" フィールドを docs.body にそのまま
使っていたが、この "text" フィールドは Wikipedia の折りたたみ(collapsible)セクション
(`{{hidden begin}}`〜`{{hidden end}}` 等)を検索インデックスから除外しており、
折りたたみ内の表(例: 「ブラタモリ」の放送回一覧)が本文に一切含まれない欠落があった。
そのため標準 XML ダンプ(`<wiki_id>-<date>-pages-articles.xml.bz2`、MediaWiki エクスポート
形式)+ wikitext 解析(mwparserfromhell)に切り替えている。折りたたみテンプレートは通常の
テンプレート呼び出しとして wikicode 木に残るため、本文抽出時に自然に含まれるようになる。

データ形式: MediaWiki エクスポート XML(<mediawiki><page><title>…</title><ns>0</ns>
<id>…</id>[<redirect title="…"/>]<revision><id>…</id><timestamp>…</timestamp>
<text>wikitext</text></revision></page>…)。xmlns の URI はダンプのスキーマ版で変わり
うるため、タグ名は名前空間を無視した局所名で比較する(`_local`)。

リダイレクトは XML 上ではリダイレクト元ページに `<redirect title="対象タイトル"/>` が
付く形で表現される(CirrusSearch の `redirect` フィールドとは向きが逆)。そのため
`ingest/sources/osm.py` の relation 2 パス走査と同じ精神で、パス1でリダイレクト元→対象の
対応を集めて `対象タイトル → [リダイレクト元タイトル, …]` に反転し、パス2で本体の Doc を
yield する際に aliases として付与する。
"""
from __future__ import annotations

import bz2
import gzip
import logging
import os
import re
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import IO, Iterator

import mwparserfromhell as mwp
from mwparserfromhell.nodes import Heading

from core import Doc

log = logging.getLogger(__name__)

DUMP_INDEX_URL = "https://dumps.wikimedia.org/{wiki_id}/"
PAGEVIEW_INDEX_URL = "https://dumps.wikimedia.org/other/pageview_complete/monthly/"

# ページビュー突合用の wiki_id → pageview_complete のドメインコード対応表。
WIKI_DOMAIN = {
    "jawiki": "ja.wikipedia",
    "enwiki": "en.wikipedia",
}

# 記事名前空間(ns=0)の通常リンクとして数えない、非表示にすべきプレフィックス。
_NON_ARTICLE_LINK_PREFIXES = (
    "File:", "ファイル:", "画像:", "Image:",
    "Category:", "カテゴリ:",
)
_CATEGORY_PREFIXES = ("Category:", "カテゴリ:")

# Wikimedia は User-Agent の無い/汎用スクリプト由来のリクエストを 403 で拒否するため、
# 連絡先付きの UA を明示する(https://meta.wikimedia.org/wiki/User-Agent_policy)。
USER_AGENT = "chiezo-ingest/0.1 (https://github.com/; contact via repo issues)"


def _http_get(url: str, timeout: int = 60) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _local(tag: str) -> str:
    """`{namespace-uri}tagname` から namespace を取り除いた局所タグ名を返す。"""
    return tag.rsplit("}", 1)[-1]


def _child(elem: ET.Element, name: str) -> ET.Element | None:
    """直接の子要素を局所タグ名で探す(xmlns の URI を気にしない)。"""
    for c in elem:
        if _local(c.tag) == name:
            return c
    return None


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
        self._pageview_path: Path | None = None
        self._pageview_period: str | None = None

    # ---- 取得 -------------------------------------------------------------

    def latest_dump_date(self) -> str:
        """ダンプ親ディレクトリの一覧から最新日付を得る。DUMP_DATE 環境変数で上書き可。"""
        if date := os.environ.get("DUMP_DATE"):
            return date
        html = _http_get(DUMP_INDEX_URL.format(wiki_id=self.source))
        dates = sorted(set(re.findall(r'href="(\d{8})/"', html)))
        if not dates:
            raise RuntimeError(f"no dump date directories found for {self.source}")
        return dates[-1]

    def _dump_filename(self, date: str) -> str:
        return f"{self.source}-{date}-pages-articles.xml.bz2"

    def fetch(self, workdir: Path) -> tuple[Path, str]:
        """記事本文 XML ダンプを取得しローカルパスとダンプ日付を返す。curl -C - で再開可能。"""
        date = self.latest_dump_date()
        filename = self._dump_filename(date)
        dest = workdir / filename
        part = dest.with_suffix(dest.suffix + ".part")
        if not (dest.exists() and not part.exists()):
            url = f"{DUMP_INDEX_URL.format(wiki_id=self.source)}{date}/{filename}"
            log.info("downloading %s", url)
            subprocess.run(
                ["curl", "-fSL", "-A", USER_AGENT, "--retry", "5", "-C", "-", "-o", str(part), url],
                check=True,
            )
            part.rename(dest)
        return dest, date

    def latest_pageview_period(self) -> str:
        """pageview_complete/monthly の最新年月(YYYY-MM)を得る。PAGEVIEW_PERIOD 環境変数で上書き可。"""
        if period := os.environ.get("PAGEVIEW_PERIOD"):
            return period
        html = _http_get(PAGEVIEW_INDEX_URL)
        years = sorted(set(re.findall(r'href="(\d{4})/"', html)))
        if not years:
            raise RuntimeError("no pageview_complete year directories found")
        year = years[-1]
        html = _http_get(f"{PAGEVIEW_INDEX_URL}{year}/")
        months = sorted(set(re.findall(rf'href="({re.escape(year)}-\d{{2}})/"', html)))
        if not months:
            raise RuntimeError(f"no pageview_complete month directories found for {year}")
        return months[-1]

    def fetch_pageviews(self, workdir: Path) -> Path | None:
        """月次ページビュー(bot 除外・全プロジェクト合算)を取得する。

        対応表に無い wiki_id(WIKI_DOMAIN 未登録)ではページビュー突合をスキップする。
        ファイルは全 Wikimedia プロジェクト合算(圧縮 5〜6GB)で、対象ドメインへの
        絞り込みは _load_pageviews 側でストリーミング中に行う(全体を先には絞れない)。
        """
        domain = WIKI_DOMAIN.get(self.source)
        if domain is None:
            log.info("no pageview domain mapping for %s; skipping pageviews", self.source)
            return None
        period = self.latest_pageview_period()
        year, _, _ = period.partition("-")
        filename = f"pageviews-{period.replace('-', '')}-user.bz2"
        dir_url = f"{PAGEVIEW_INDEX_URL}{year}/{period}/"
        dest = workdir / filename
        part = dest.with_suffix(".bz2.part")
        if not (dest.exists() and not part.exists()):
            url = dir_url + filename
            log.info("downloading %s", url)
            subprocess.run(
                ["curl", "-fSL", "-A", USER_AGENT, "--retry", "5", "-C", "-", "-o", str(part), url],
                check=True,
            )
            part.rename(dest)
        self._pageview_path = dest
        self._pageview_period = period
        return dest

    def _load_pageviews(self) -> dict[int, int]:
        """page_id → 月間合計閲覧数(アクセス種別 desktop/mobile-web/mobile-app 合算)。

        pageview_complete はドメインコードでアルファベット順にソートされているため、
        対象ドメインの行を通過し終えたら走査を打ち切れる(全体を読み切らずに済む)。
        """
        domain = WIKI_DOMAIN.get(self.source)
        if domain is None or self._pageview_path is None:
            return {}
        counts: dict[int, int] = {}
        seen = False
        with bz2.open(self._pageview_path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.rstrip("\n").split(" ", 5)
                if len(parts) < 5 or parts[0] != domain:
                    if seen:
                        break
                    continue
                seen = True
                page_id_str = parts[2]
                if page_id_str == "null":
                    continue
                try:
                    page_id = int(page_id_str)
                    total = int(parts[4])
                except ValueError:
                    continue
                counts[page_id] = counts.get(page_id, 0) + total
        log.info("loaded pageviews for %d pages (%s, %s)", len(counts), domain, self._pageview_period)
        return counts

    # ---- XML ストリーミング -------------------------------------------------

    @staticmethod
    def _opener(path: Path) -> IO[bytes]:
        if path.suffix == ".bz2":
            return bz2.open(path, "rb")
        return gzip.open(path, "rb")

    def _iter_pages(self, path: Path) -> Iterator[ET.Element]:
        """`<page>` 要素をストリーミングで yield する。yield 後は要素をクリアしメモリを解放する。"""
        with self._opener(path) as f:
            context = ET.iterparse(f, events=("start", "end"))
            _, root = next(context)
            for event, elem in context:
                if event == "end" and _local(elem.tag) == "page":
                    yield elem
                    elem.clear()
                    root.clear()

    # ---- 変換 -------------------------------------------------------------

    def iter_docs(self, path: Path) -> Iterator[Doc]:
        """XML ダンプを 2 パスでストリーミングし、コアスキーマの Doc を yield する。

        パス1: リダイレクトページ(ns=0 かつ <redirect> あり)を集め、
        対象タイトル → [リダイレクト元タイトル, …] の逆引き辞書を作る。
        パス2: リダイレクトでない ns=0 ページを Doc として yield する(aliases 込み)。
        fetch_pageviews() 済みなら、docs.extra に月間閲覧数を載せる。
        """
        redirect_targets = self._collect_redirects(path)
        pageviews = self._load_pageviews()
        for elem in self._iter_pages(path):
            ns_elem = _child(elem, "ns")
            if ns_elem is None or (ns_elem.text or "0") != "0":
                continue
            if _child(elem, "redirect") is not None:
                continue
            title_elem = _child(elem, "title")
            id_elem = _child(elem, "id")
            revision = _child(elem, "revision")
            if title_elem is None or id_elem is None or revision is None:
                continue
            text_elem = _child(revision, "text")
            wikitext = text_elem.text if text_elem is not None else None
            if not wikitext:
                continue
            title = title_elem.text or ""
            doc_id = int(id_elem.text)
            timestamp_elem = _child(revision, "timestamp")
            opening, body, tags, links = _extract_plaintext(wikitext)
            views = pageviews.get(doc_id)
            extra = (
                {"pageviews_month": views, "pageviews_period": self._pageview_period}
                if views is not None
                else None
            )
            yield Doc(
                doc_id=doc_id,
                title=title,
                opening=opening,
                body=body,
                tags=tags,
                links=links,
                aliases=redirect_targets.get(title, []),
                updated_at=timestamp_elem.text if timestamp_elem is not None else None,
                rank_score=0.0,  # XML ダンプには CirrusSearch の popularity_score 相当が無い
                extra=extra,
            )

    def _collect_redirects(self, path: Path) -> dict[str, list[str]]:
        """対象タイトル → [リダイレクト元タイトル, …] の辞書を作る(パス1)。"""
        targets: dict[str, list[str]] = {}
        for elem in self._iter_pages(path):
            ns_elem = _child(elem, "ns")
            if ns_elem is None or (ns_elem.text or "0") != "0":
                continue
            redirect_elem = _child(elem, "redirect")
            if redirect_elem is None:
                continue
            target = redirect_elem.get("title")
            title_elem = _child(elem, "title")
            if not target or title_elem is None or not title_elem.text:
                continue
            targets.setdefault(target, []).append(title_elem.text)
        return targets


def _extract_plaintext(wikitext: str) -> tuple[str | None, str | None, list[str], list[str]]:
    """wikitext から (opening, body, tags, links) を抽出する。

    opening は最初の見出しより前の節(lead section)、body は記事全体をプレーンテキスト化
    したもの。{{hidden begin}}/{{hidden end}} 等の折りたたみテンプレートは通常のテンプレート
    呼び出しとして扱われるため、中身(表を含む)は body に自然に含まれる。
    """
    code = mwp.parse(wikitext)

    lead_nodes = []
    for node in code.nodes:
        if isinstance(node, Heading):
            break
        lead_nodes.append(node)
    opening = mwp.wikicode.Wikicode(lead_nodes).strip_code().strip() or None

    body = code.strip_code(keep_template_params=True).strip() or None

    tags: list[str] = []
    links: list[str] = []
    for link in code.filter_wikilinks():
        title = str(link.title).strip()
        if not title:
            continue
        if title.startswith(_CATEGORY_PREFIXES):
            tags.append(title.split(":", 1)[1].strip())
        elif title.startswith(_NON_ARTICLE_LINK_PREFIXES):
            continue
        else:
            links.append(title)

    return opening, body, tags, links
