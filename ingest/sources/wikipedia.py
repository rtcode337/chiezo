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

from core import POPULARITY_LOG_MAX_PAGEVIEWS, Doc, normalized_popularity
from lookup import EMPTY, DiskLookup, DiskMultiMap, EmptyLookup

log = logging.getLogger(__name__)

DUMP_INDEX_URL = "https://dumps.wikimedia.org/{wiki_id}/"
PAGEVIEW_INDEX_URL = "https://dumps.wikimedia.org/other/pageview_complete/monthly/"

# ページビュー突合用の wiki_id → pageview_complete のドメインコード対応表(不規則な
# wiki の上書き用)。通常は URL 言語コード(lang)から `<lang>.wikipedia` と機械的に
# 導出できる(zh_yuewiki は lang="zh-yue" → zh-yue.wikipedia)ので、ここに書くのは
# その導出が合わない wiki だけでよい。
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

# page_props SQL ダンプ中の `(<page_id>,'wikibase_item','Q123',...)` を拾う。
_WIKIBASE_ITEM_RE = re.compile(r"\((\d+),'wikibase_item','(Q\d+)'")

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

    # 巨大な対応表(リダイレクト・ページビュー・wikidata)は lookup.py でディスクに逃がして
    # あるため、wikipedia 系の常駐は SQLite のページキャッシュ(512MiB)+ wikitext 解析の
    # 一時オブジェクトが主。実測ピークは 1GiB 未満だが、FTS 構築ぶんの余裕を見て 3GiB とする。
    min_build_memory_gb = 3.0

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
        self._page_props_path: Path | None = None

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

    def pageview_domain(self) -> str | None:
        """pageview_complete のドメインコード。

        WIKI_DOMAIN の明示(不規則な wiki 用)があればそれを、無ければ URL 言語コード
        から `<lang>.wikipedia` を機械的に導出する(lang は URL のサブドメインなので
        pageview_complete のドメインコードと一致する)。
        """
        explicit = WIKI_DOMAIN.get(self.source)
        if explicit:
            return explicit
        return f"{self.lang}.wikipedia" if self.lang else None

    def fetch_pageviews(self, workdir: Path) -> Path | None:
        """月次ページビュー(bot 除外・全プロジェクト合算)を取得する。

        ドメインコードを導出できない wiki ではページビュー突合をスキップする。
        ファイルは全 Wikimedia プロジェクト合算(圧縮 5〜6GB)で、対象ドメインへの
        絞り込みは _load_pageviews 側でストリーミング中に行う(全体を先には絞れない)。
        """
        domain = self.pageview_domain()
        if domain is None:
            log.info("no pageview domain for %s; skipping pageviews", self.source)
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

    def fetch_page_props(self, workdir: Path) -> Path | None:
        """page_props ダンプ(記事 → wikidata の Q 番号の対応表)を取得する。

        ダンプ日付は本体 XML と同じディレクトリのものを使う(latest_dump_date は
        DUMP_DATE 環境変数で固定できるため、本体と食い違うことはない)。
        """
        date = self.latest_dump_date()
        filename = f"{self.source}-{date}-page_props.sql.gz"
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
        self._page_props_path = dest
        return dest

    def _load_page_props(self) -> DiskLookup | EmptyLookup:
        """page_id → wikidata の Q 番号。

        page_props は MediaWiki の SQL ダンプ(巨大な INSERT 文の羅列)なので、
        行として読まず `(page_id,'propname','value',...)` のタプルを正規表現で拾う。
        必要なのは propname='wikibase_item' の行だけ。

        ja Wikipedia では 186 万件あり dict で持つと約 270MiB を常駐で占める。
        取り込みループからの点引きにしか使わないため、ディスク上の一時 SQLite に置く
        (`lookup.DiskLookup`。詳しい理由は同モジュールの docstring)。
        """
        if self._page_props_path is None:
            return EMPTY
        props = DiskLookup(self._page_props_path.with_suffix(".lookup.db"))
        with gzip.open(self._page_props_path, "rt", encoding="utf-8", errors="replace") as f:
            for chunk in f:
                props.extend(
                    (int(page_id), qid) for page_id, qid in _WIKIBASE_ITEM_RE.findall(chunk)
                )
        props.finish()
        log.info("loaded wikidata ids for %d pages", len(props))
        return props

    def _load_pageviews(self) -> DiskLookup | EmptyLookup:
        """page_id → 月間合計閲覧数(アクセス種別 desktop/mobile-web/mobile-app 合算)。

        pageview_complete はドメインコードでアルファベット順にソートされているため、
        対象ドメインの行を通過し終えたら走査を打ち切れる(全体を読み切らずに済む)。

        件数が数百万に達し dict では数百 MiB を常駐で占めるため、page_props と同様に
        ディスク上の一時 SQLite へ置く(1 ページが access_method ごとに複数行へ
        分かれているので `accumulate=True` で合算する)。
        """
        domain = self.pageview_domain()
        if domain is None or self._pageview_path is None:
            return EMPTY
        counts = DiskLookup(
            self._pageview_path.with_suffix(".lookup.db"), accumulate=True
        )
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
                counts.add(page_id, total)
        counts.finish()
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
        fetch_pageviews() / fetch_page_props() 済みなら、docs.extra に月間閲覧数と
        wikidata の Q 番号を載せる。

        3 つの対応表(リダイレクト・ページビュー・wikidata)はいずれも件数が百万単位で、
        メモリに載せるとホストごと OOM を招くためディスク上の一時 SQLite に置く
        (`ingest/lookup.py`)。中断時にも消えるよう finally で必ず後始末する。
        """
        redirect_targets = self._collect_redirects(path)
        pageviews = self._load_pageviews()
        wikidata_ids = self._load_page_props()
        try:
            yield from self._iter_docs(path, redirect_targets, pageviews, wikidata_ids)
        finally:
            for lookup in (redirect_targets, pageviews, wikidata_ids):
                lookup.close()

    def _iter_docs(self, path: Path, redirect_targets, pageviews, wikidata_ids) -> Iterator[Doc]:
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
            opening, body, tags, links, coords = _extract_plaintext(wikitext)
            extra: dict = {}
            if coords is not None:
                extra["lat"], extra["lon"] = coords
            if (views := pageviews.get(doc_id)) is not None:
                extra["pageviews_month"] = views
                extra["pageviews_period"] = self._pageview_period
            if qid := wikidata_ids.get(doc_id):
                extra["wikidata"] = qid
            yield Doc(
                doc_id=doc_id,
                title=title,
                opening=opening,
                body=body,
                tags=tags,
                links=links,
                aliases=redirect_targets.get(title),
                updated_at=timestamp_elem.text if timestamp_elem is not None else None,
                # XML ダンプには CirrusSearch の popularity_score 相当が無いので、
                # 突合した月間ページビューを正規化して知名度として使う(取れなければ 0)。
                rank_score=normalized_popularity(
                    extra.get("pageviews_month"), POPULARITY_LOG_MAX_PAGEVIEWS
                ),
                extra=extra or None,
            )

    def _collect_redirects(self, path: Path) -> DiskMultiMap:
        """対象タイトル → [リダイレクト元タイトル, …] の対応表を作る(パス1)。

        ja Wikipedia のリダイレクトは 160 万件あり、`dict[str, list[str]]` に貯めると
        文字列とリストのオーバーヘッドで GB 級に達する。パス2 の本文解析と同時に
        生きているため、これがホストの OOM の主因だった。ディスクへ逃がす。
        """
        targets = DiskMultiMap(path.with_suffix(".redirects.db"))
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
            targets.add(target, title_elem.text)
        targets.finish()
        log.info("collected redirects for %d titles", len(targets))
        return targets


def _is_float(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def _dms(parts: list[str], hemisphere: str) -> float | None:
    """度・分・秒(可変長)と N/S/E/W から十進度を作る。"""
    if not parts or not all(_is_float(p) for p in parts):
        return None
    degrees = 0.0
    for i, part in enumerate(parts[:3]):
        degrees += float(part) / (60**i)
    return -degrees if hemisphere in ("S", "W") else degrees


def _coords_from_positional(template) -> tuple[float, float] | None:
    """{{Coord|35|39|31|N|139|41|30|E|...}} / {{Coord|35.65|139.69|...}} 形式。

    度分秒は 2〜3 個と可変なので、方位(N/S)の出現位置で緯度側の要素数を決め、
    経度側も同じ数だけ読む。`type:city` のような修飾子以降は無視する。
    """
    values: list[str] = []
    for param in template.params:
        if param.showkey:
            break
        value = str(param.value).strip()
        if not value or ":" in value:
            break
        values.append(value)
    upper = [v.upper() for v in values]
    for i, value in enumerate(upper):
        if value in ("N", "S"):
            lat = _dms(values[:i], value)
            tail = upper[i + 1 :]
            if len(tail) < i + 1 or tail[i] not in ("E", "W"):
                return None
            lon = _dms(values[i + 1 : i + 1 + i], tail[i])
            return (lat, lon) if lat is not None and lon is not None else None
    if len(values) >= 2 and _is_float(values[0]) and _is_float(values[1]):
        return float(values[0]), float(values[1])
    return None


# 名前付き引数で座標を持つテンプレート(日本の駅・空港の Infobox 等)の引数名。
# (度, 分, 秒, 方位) の順。秒・方位は省略されることがある。
_NAMED_COORD_KEYS = [
    (("緯度度", "緯度分", "緯度秒", "南北"), ("経度度", "経度分", "経度秒", "東西")),
    (("latd", "latm", "lats", "latNS"), ("longd", "longm", "longs", "longEW")),
    (("lat_deg", "lat_min", "lat_sec", "lat_dir"), ("lon_deg", "lon_min", "lon_sec", "lon_dir")),
]
# 十進度をそのまま持つ引数名
_DECIMAL_COORD_KEYS = [("緯度", "経度"), ("latitude", "longitude"), ("lat", "lon"), ("lat", "long")]


def _coords_from_named(template) -> tuple[float, float] | None:
    def value_of(key: str) -> str | None:
        if not template.has(key):
            return None
        return str(template.get(key).value).strip() or None

    for lat_keys, lon_keys in _NAMED_COORD_KEYS:
        lat_parts = [v for v in (value_of(k) for k in lat_keys[:3]) if v]
        lon_parts = [v for v in (value_of(k) for k in lon_keys[:3]) if v]
        if not lat_parts or not lon_parts:
            continue
        lat = _dms(lat_parts, (value_of(lat_keys[3]) or "N").upper())
        lon = _dms(lon_parts, (value_of(lon_keys[3]) or "E").upper())
        if lat is not None and lon is not None:
            return lat, lon
    for lat_key, lon_key in _DECIMAL_COORD_KEYS:
        lat_raw, lon_raw = value_of(lat_key), value_of(lon_key)
        if lat_raw and lon_raw and _is_float(lat_raw) and _is_float(lon_raw):
            return float(lat_raw), float(lon_raw)
    return None


def _extract_coordinates(code) -> tuple[float, float] | None:
    """記事の wikitext から代表座標を取り出す(最初に見つかった妥当な 1 組)。

    ja Wikipedia の座標は {{Coord}} 系テンプレートのほか、駅・空港・施設の Infobox が
    持つ `緯度度`/`経度度` のような名前付き引数にも入っている。両方を拾う。
    Wikidata の P625 を引くには別途巨大なダンプが要るため、ここでは本文だけで完結させる。
    """
    for template in code.filter_templates():
        name = str(template.name).strip().lower()
        if name.startswith(("coord", "座標", "ウィキ座標")):
            coords = _coords_from_positional(template) or _coords_from_named(template)
        else:
            coords = _coords_from_named(template)
        if coords is None:
            continue
        lat, lon = coords
        # 0,0(未入力のプレースホルダ)や範囲外は捨てる
        if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0.0, 0.0):
            return round(lat, 7), round(lon, 7)
    return None


def _extract_plaintext(
    wikitext: str,
) -> tuple[str | None, str | None, list[str], list[str], tuple[float, float] | None]:
    """wikitext から (opening, body, tags, links, coords) を抽出する。

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

    return opening, body, tags, links, _extract_coordinates(code)
