"""外向きの道具 —— RSS / Atom を機械的に取ってきて、AI に**参考として**渡す。

## 何のためか

`app/extract.py` が手元の長期記憶から機械的に引く道具なら、こちらは外から引く道具。
どちらも狙いは同じで、**AI が間違えるところと、間違えないところを分ける**こと ——
見出しと URL と日付は機械が正確に取れる。何が重要でどう束ねるかは取れない。

## 使い道は 2 つある

**1. AI への素材として差し込む**(`{feed}`)。何を DB へ入れるかは AI が決める ——
自分でも web を検索し、渡されたぶんも含めて採否を判断する。だから道具が
取りこぼしても穴にはならないし、道具が拾った宣伝記事がそのまま溜まることもない。

**2. 機械的にそのまま溜める**(巡回の引き方が「外の道具で引く」のとき)。
`app/extract.py` が手元の名簿を機械で埋めるのと同じ役割で、**見出し・要約・URL・
配信日という、機械が正確に取れるものだけ**を入れる。AI は呼ばない。

**どちらを使うかは巡回が決める。** 2 は「取りこぼしたものは入らない」「拾った宣伝記事も
入る」を引き受ける代わりに、**枠を使わずに毎時回せる**。判断の要る仕事(重要度を付ける、
まとめる、漏れを探す)は別の巡回が AI に頼む —— 名簿と肉付けを分けるのと同じ形。

## 外へ出る以上は守る(`app/websearch.py` と同じ契約)

- **本文は取りに行かない。** 取るのはフィードが配っている見出し・要約・URL・日付だけ。
  ページを取得して中身を読むのはスクレイピングに踏み込む話で、相手への負担も
  壊れやすさも別次元になる
- **自分でレート制限をかける**(`MIN_INTERVAL`)。無人で回る層なので、
  こちらが加減しないと相手のログに等間隔の足跡だけが延々と残る
- **`User-Agent` に個人情報を入れない。** 名乗るのはプロジェクト名だけ
- **DOCTYPE のある XML は読まない**(`_looks_unsafe`)。実体参照の展開で
  メモリを食い尽くす細工に付き合わないため。フィードに DTD が付くことは実際まず無い

**外の URL を叩けるのは、有効にした収集だけ。** 定義は誰でも置けるが、走り出すのは
Chiezo の管理画面で有効にしたときだけなので、そこが唯一の関門になる
(`enabled` を REST から触れなくしてあるのと同じ線)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from itertools import zip_longest
from urllib.parse import quote, urlparse
from xml.etree import ElementTree

import httpx
from fastapi import HTTPException

log = logging.getLogger("chiezo.app")

# 相手への礼儀としての最小間隔(秒)。`app/websearch.py` と同じ値・同じ理由。
MIN_INTERVAL = 1.0
# 名乗り。プロジェクト名だけを入れる(連絡先・個人名・メールアドレスは入れない)。
USER_AGENT = "chiezo (local knowledge server)"
TIMEOUT = 15.0

# 1 つの収集が持てるフィードの数(`{query}` を展開したあとの本数も含む)。
# **増やすほど 1 回の実行が遅くなる**(順に取るので、40 本で最短 40 秒)。
# **検索の RSS を検索文 × ページで並べる**収集があるので 20 では足りなかった ——
# 人気順の 2〜3 ページ目に本を並べた定番の記事が集まっており、1 ページ目だけでは
# 98 件入っても、2 本以上の記事に出てくる本が 2 冊しか無かった
# (検索文 8 つ × 人気順 3 ページ + 新着 1 ページで 32 本)。機械で拾う回は数時間〜1 日おきに
# 回すので、40 秒は許せる長さ
MAX_URLS = 40
# 受け取る見出しの数の天井(全フィード合わせて。指定の `limit` をここで頭打ちにする)。
# **`{feed}` へ差し込む素材なら多くしても AI の読む量が増えるだけ**なので、既定
# (`DEFAULT_LIMIT`)は小さく置く。**天井は機械で溜める回に合わせる** —— 検索文 ×
# ページで 32 本を引く収集が 200 で切られると、1 本あたり数件しか残らず、ページを
# 深く引いた意味が消える(40 本 × 1 本 40 件まで入る高さ)
MAX_ITEMS = 1600
DEFAULT_LIMIT = 60
# 1 本ぶんに読む大きさ。**大きすぎるものは読まない** —— フィードは索引であって
# 全文の配布物ではないので、これを超えるものは相手の設定がおかしい
MAX_BYTES = 2 * 1024 * 1024
# 1 件の要約の長さ。長い本文を丸ごと載せる相手がいるので切る
MAX_SUMMARY_CHARS = 300
# 1 本のフィードに付けられるタグの数。**束の名前を書くためのもの**で、
# 分類をここで済ませるためのものではない(それは AI の巡回の仕事)
MAX_TAGS = 5

# 1 件から拾う「配信元が付けた語」の上限。10 個並べる配信元があり、
# そのまま出すと見出しより長い行になる
MAX_SUBJECTS = 5

# 前回の実行より後のものだけを渡す、の印
SINCE_LAST_RUN = "last_run"

# URL の中の検索文の差し込み口(`app/search_queries.py`)。**書いた URL は、
# 回ごとの検索文の数だけ展開される** —— 同じ検索文で引き直しても同じ記事が返るだけなので
QUERY_SLOT = "{query}"
# 検索文で入った文書に付ける印の既定(`<印>:<検索文>`)。次の回にどれが当たったかを数える
DEFAULT_QUERY_TAG = "検索"
# 使った検索文を、また使えるようになるまでの日数の既定。**二度と使えない形にはしない** ——
# 記事は日々増えるので、しばらく経てば同じ検索文でも新しい記事が当たる
DEFAULT_REUSE_DAYS = 60

_ATOM = "{http://www.w3.org/2005/Atom}"
# RSS 1.0(RDF)。**`<item>` が `<channel>` の外に並ぶ** —— RSS 2.0 のつもりで
# `channel` の下だけを見ていると、この形の配信元は丸ごと 0 件になる
# (実際、いちばん件数の多い配信元が一件も入っていなかった)
_RSS1 = "{http://purl.org/rss/1.0/}"
# 1 件ごとの絵。配信元によって置き場が違う(どれも「フィードが配っているもの」で、
# ページを取りに行くわけではない)
_MEDIA = "{http://search.yahoo.com/mrss/}"
_HATENA = "{http://www.hatena.ne.jp/info/xmlns#}"

_last_call = 0.0
_lock = asyncio.Lock()


# JSON の配信元で、どの項目を読むかを書ける鍵(`json`)。**RSS / Atom の 1 件と同じ形**
# に写す —— 後ろの工程から見れば、どちらから来たのかは区別が付かない
JSON_FIELDS = ("items", "title", "url", "summary", "at", "image", "subjects", "from")


def _bad(message: str) -> HTTPException:
    return HTTPException(400, {"error": f"フィードの指定が読めません: {message}"})


def _one_url(raw) -> dict:
    """1 本ぶんの指定。**ただの URL でも、タグ付きのオブジェクトでも書ける**。

    タグを書けるようにしてあるのは、機械で溜めるときに**どの束のものか**を
    後から引けるようにするため —— ニュースと記事と論文が同じところに溜まっても、
    タグで分けて取り出せる。フィード自身の名前(配信元)は取ってきたときに分かるので、
    ここに書くのは配信元では表せない区別だけでよい。
    """
    if isinstance(raw, str):
        raw = {"url": raw}
    if not isinstance(raw, dict):
        raise _bad("urls には URL の文字列か、url を持つオブジェクトを書いてください")
    url = str(raw.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise _bad(f"http(s) の URL を書いてください: {url or '(空)'}")
    tags = [str(t).strip() for t in (raw.get("tags") or []) if str(t).strip()]
    if len(tags) > MAX_TAGS:
        raise _bad(f"1 本に付けられるタグは {MAX_TAGS} 個まで")
    out = {"url": url, "tags": tags[:MAX_TAGS]}
    if (mapping := raw.get("json")) not in (None, {}):
        out["json"] = _json_mapping(mapping)
    return out


def _json_mapping(raw) -> dict:
    """JSON の配信元の読み方(`json`)。**RSS を配っていない相手のため**の口。

    たとえば話題の論文(Hugging Face の Daily Papers)は JSON の API しか配っておらず、
    RSS / Atom だけを読む道具では入れようがなかった。値は 2 通りに書ける ——

    - `"paper.title"` …… 1 件の中の項目を、ドットでたどって読む
    - `"https://arxiv.org/abs/{paper.id}"` …… `{…}` の中を同じようにたどって埋める

    `items` は 1 件の並びがある場所(空なら応答そのものが並び)。`from` は配信元の
    名前をそのまま書く(JSON には RSS のようなフィードの題名が無い)。
    **title と url は必須** —— どちらかが無いと、重複の鍵も出典も作れない。
    """
    if not isinstance(raw, dict):
        raise _bad("json には、どの項目を読むかをオブジェクトで書いてください")
    unknown = [k for k in raw if k not in JSON_FIELDS]
    if unknown:
        raise _bad(f"json に書けるのは {', '.join(JSON_FIELDS)} だけです(読めない鍵: {', '.join(unknown)})")
    mapping = {k: str(v).strip() for k, v in raw.items() if isinstance(v, str)}
    if not mapping.get("title") or not mapping.get("url"):
        raise _bad("json には title と url を書いてください")
    return mapping


def normalize(raw) -> dict | None:
    """指定を検証して、欠けている鍵を埋めた形にする。無ければ None。

    **読めない指定は黙って無視せず断る**(`app/extract.py` と同じ判断)。
    無視すると、道具を付けたつもりの収集が道具なしで回り続ける。
    """
    if raw in (None, "", {}):
        return None
    if not isinstance(raw, dict):
        raise _bad("オブジェクトで書いてください")
    urls = [_one_url(u) for u in (raw.get("urls") or []) if u]
    if not urls:
        raise _bad("urls に 1 本以上書いてください")
    if len(urls) > MAX_URLS:
        raise _bad(f"urls は {MAX_URLS} 本まで(いまは {len(urls)} 本)")
    try:
        limit = int(raw.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        raise _bad("limit は数で書いてください") from None
    out = {
        "urls": urls,
        "limit": max(1, min(limit, MAX_ITEMS)),
        # 前回より後のものだけにするか。**既定は絞らない** —— 日付を持たない
        # フィードが普通にあり、絞ると静かに 0 件になる
        "since": SINCE_LAST_RUN if raw.get("since") == SINCE_LAST_RUN else None,
    }
    return {**out, **_query_part(raw, urls)}


def _query_part(raw: dict, urls: list[dict]) -> dict:
    """検索文の差し込み口まわり(`{query}` を書いた URL があるときだけ)。

    **展開したあとの本数も `MAX_URLS` に収める** —— 1 回に使える検索文の数は
    「差し込み口の無い URL を除いた残り ÷ 差し込み口のある URL の数」まで。
    """
    templates = sum(1 for one in urls if QUERY_SLOT in one["url"])
    if not templates:
        return {}
    room = (MAX_URLS - (len(urls) - templates)) // templates
    if room < 1:
        raise _bad(f"{QUERY_SLOT} を書いた URL が多すぎます(展開すると {MAX_URLS} 本を超える)")
    seed = [" ".join(str(q).split()) for q in (raw.get("queries") or []) if str(q).strip()]
    if not seed:
        raise _bad(f"{QUERY_SLOT} を使うなら、最初の組の検索文を queries に書いてください")
    try:
        per_run = int(raw.get("per_run") or room)
    except (TypeError, ValueError):
        raise _bad("per_run は数で書いてください") from None
    tag = str(raw.get("query_tag") or DEFAULT_QUERY_TAG).strip() or DEFAULT_QUERY_TAG
    try:
        reuse_days = int(raw.get("reuse_days") if raw.get("reuse_days") is not None else DEFAULT_REUSE_DAYS)
    except (TypeError, ValueError):
        raise _bad("reuse_days は数で書いてください") from None
    return {
        "queries": seed,
        "query_tag": tag,
        "per_run": max(1, min(per_run, room)),
        # 0 は「すぐにまた使ってよい」(毎回同じ検索文でもよい収集のため)
        "reuse_days": max(0, reuse_days),
    }


def has_templates(spec: dict | None) -> bool:
    """検索文の差し込み口を持つ指定か。"""
    return bool(spec and spec.get("queries"))


def expand(spec: dict, queries: list[str]) -> dict:
    """差し込み口に検索文を入れて、**検索文の数だけ URL を並べた**指定にする。

    展開した 1 本には `<印>:<検索文>` のタグを足す —— 次の回に、どの検索文が
    何件連れてきたかを数える(`search_queries.count`)。**URL の中へは符号化して入れる**
    (空白や日本語をそのまま入れると、相手に届く前に壊れる)。
    """
    urls: list[dict] = []
    for one in spec["urls"]:
        if QUERY_SLOT not in one["url"]:
            urls.append(one)
            continue
        for q in queries:
            urls.append({
                "url": one["url"].replace(QUERY_SLOT, quote(q, safe="")),
                "tags": [*one["tags"], f"{spec['query_tag']}:{q}"][: MAX_TAGS + 1],
                **({"json": one["json"]} if one.get("json") else {}),
            })
    return {**spec, "urls": urls}


def to_json(spec: dict | None) -> dict | None:
    """定義のメモへ書ける形。**空の鍵は落とす**(読むときに邪魔なだけ)。

    タグを付けていない出典は **URL の文字列のまま書く** —— 読むのは人なので、
    付いていない鍵を並べるだけの `{"url": …, "tags": []}` にはしない。
    """
    if not spec:
        return None
    out = {k: v for k, v in spec.items() if v not in (None, "")}
    out["urls"] = [
        one["url"] if not one["tags"] and not one.get("json") else one for one in spec["urls"]
    ]
    return out


async def _throttle() -> None:
    """呼ばれた回数ぶん素直に外へ出さない(最小間隔を空ける)。"""
    global _last_call
    async with _lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = time.monotonic()


def _client() -> httpx.AsyncClient:
    """フィード向けの HTTP クライアント(テストはここを差し替える)。"""
    return httpx.AsyncClient(
        timeout=TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml"},
        follow_redirects=True,
    )


async def fetch(spec: dict, since: str | None = None) -> dict:
    """指定のフィードを順に引いて、見出しの一覧にする。

    **落ちても例外にしない。** これは参考の素材で、AI は自分でも調べる ——
    取れなかったことを伝えて先へ進むほうが、1 本の不調で収集を止めるよりよい
    (`app/websearch.py` と同じ扱い)。取れなかった相手は数だけ返す。
    """
    cutoff = _parse(since) if spec["since"] == SINCE_LAST_RUN else None
    per_source: list[list[dict]] = []
    failed = 0
    for one in spec["urls"]:
        await _throttle()
        try:
            entries = await _fetch_one(one["url"], one.get("json"))
        except (httpx.HTTPError, ElementTree.ParseError, ValueError) as e:
            # 例外の文言(接続先のホスト名等が入る)はログにだけ残す
            log.info("feed fetch failed %s: %r", one["url"], e)
            failed += 1
            continue
        entries = [{**e, "tags": one["tags"]} for e in entries]
        kept = [e for e in entries if cutoff is None or _after(e["at"], cutoff)]
        kept.sort(key=lambda e: e["at"] or "", reverse=True)
        per_source.append(kept)
    return {
        "items": _fair_share(per_source, spec["limit"]),
        "failed": failed,
        "tried": len(spec["urls"]),
    }


def _fair_share(per_source: list[list[dict]], limit: int) -> list[dict]:
    """配信元ごとに順番に取って、最後に新しい順へ並べ直す。

    **まとめてから新しい順に切ると、更新の遅い配信元が永久に入らない** ——
    速い配信元が上限を埋めてしまうため。実際に、数日おきに出る配信元が
    1 件も入らないまま回り続けていた(1 日 60 件を超える配信元が同居していた)。
    """
    taken: list[dict] = []
    for row in zip_longest(*per_source):
        for entry in row:
            if entry is not None and len(taken) < limit:
                taken.append(entry)
        if len(taken) >= limit:
            break
    # **新しい順に並べ直す。** 読む側に効くのは新しさ(どの配信元から来たかではない)
    taken.sort(key=lambda e: e["at"] or "", reverse=True)
    return taken


async def _fetch_one(url: str, mapping: dict | None = None) -> list[dict]:
    async with _client() as client:
        res = await client.get(url)
    res.raise_for_status()
    body = res.content[:MAX_BYTES]
    if mapping:
        return _json_entries(json.loads(body), mapping, url)
    if _looks_unsafe(body):
        raise ValueError("DTD のある XML は読まない")
    root = ElementTree.fromstring(body)
    source = _source_name(root, url)
    entries = _rss(root) or _atom(root)
    return [{**e, "from": source} for e in entries]


def _json_entries(data, mapping: dict, url: str) -> list[dict]:
    """JSON の応答を、RSS / Atom の 1 件と同じ形に写す(`_json_mapping`)。

    **読めない 1 件は飛ばす**(見出しか URL が取れないもの)。読めない値は空にする ——
    日付が読めなければ「日付なし」として残す(RSS と同じ扱い)。
    """
    items = _dig(data, mapping.get("items", "")) if mapping.get("items") else data
    if not isinstance(items, list):
        raise ValueError("json の items が並びではありません")
    source = mapping.get("from") or urlparse(url).netloc
    out = []
    for item in items:
        title = _pick(item, mapping.get("title", ""))
        link = _pick(item, mapping.get("url", ""))
        if not title or not link.startswith(("http://", "https://")):
            continue
        subjects = _dig(item, mapping["subjects"]) if mapping.get("subjects") else []
        out.append({
            "title": title,
            "url": link,
            "summary": _pick(item, mapping.get("summary", ""))[:MAX_SUMMARY_CHARS],
            "at": _when(_pick(item, mapping.get("at", ""))),
            "image": _pick(item, mapping.get("image", "")),
            "subjects": [
                str(s).strip() for s in (subjects if isinstance(subjects, list) else [])
                if isinstance(s, str | int) and str(s).strip()
            ][:MAX_SUBJECTS],
            "from": source,
        })
    return out


def _dig(data, path: str):
    """ドットでたどる(`paper.title`)。たどれなければ None。"""
    for key in [p for p in path.split(".") if p]:
        if isinstance(data, dict):
            data = data.get(key)
        elif isinstance(data, list) and key.isdigit() and int(key) < len(data):
            data = data[int(key)]
        else:
            return None
    return data


def _pick(item, spec: str) -> str:
    """1 件から文字を 1 つ読む。`{…}` があれば埋め込み、無ければ項目そのもの。

    **埋める値が 1 つでも無ければ空**(半端な URL を作らない)。
    """
    if not spec:
        return ""
    if "{" in spec:
        missing = False

        def fill(match):
            nonlocal missing
            value = _dig(item, match.group(1))
            if value in (None, ""):
                missing = True
                return ""
            return str(value)

        text = re.sub(r"\{([^{}]+)\}", fill, spec)
        return "" if missing else text.strip()
    value = _dig(item, spec)
    return str(value).strip() if isinstance(value, str | int | float) else ""


def _looks_unsafe(body: bytes) -> bool:
    """DTD(`<!DOCTYPE`)を持っているか。

    実体参照の展開でメモリを食い尽くす細工に付き合わないための門前払い。
    **フィードに DTD が付くことは実際まず無い**ので、落とす副作用はほぼ無い。
    """
    return b"<!DOCTYPE" in body[:4096] or b"<!ENTITY" in body[:4096]


def _subjects(item, *paths: str) -> list[str]:
    """配信元が 1 件ごとに付けている語(`<category>` / `<dc:subject>`)。

    **書き手が付けた語は、AI が推し量った分野より確か**です —— Qiita の記事に
    「Codex」と書いてあるなら、それはその記事の主役の名前。読まずに捨てていた頃は、
    **製品名がタグに一度も現れず**、そこから作るトピックの網も大きな分野ばかりに
    なっていた(実測で、上位の語は AI / セキュリティ / LLM のような広い語ばかり)。

    **数は絞る**(`MAX_SUBJECTS`)。1 件に 10 個並べる配信元があり、そのまま出すと
    見出しより長い行になる。**並びは配信元のまま** —— 先に書いてあるものほど
    主題に近い(Qiita も Zenn も、そう並べている)。
    """
    out: list[str] = []
    for path in paths:
        for found in item.findall(path):
            word = " ".join((found.text or found.get("term") or "").split())
            if word and word not in out:
                out.append(word)
    return out[:MAX_SUBJECTS]


def _text(node, *paths: str) -> str:
    for path in paths:
        found = node.find(path)
        if found is not None and (found.text or "").strip():
            return " ".join((found.text or "").split())
    return ""


def _source_name(root, url: str) -> str:
    """出典の名前。取れなければホスト名(どこから来たかは必ず出す)。"""
    channel = root.find("channel")
    if channel is None:
        channel = root.find(f"{_RSS1}channel")
    name = _text(channel, "title", f"{_RSS1}title") if channel is not None else ""
    return name or _text(root, f"{_ATOM}title") or urlparse(url).netloc


def _items(root) -> list:
    """RSS の 1 件ずつ。**RSS 1.0 は `<channel>` の外に並ぶ**ので、両方を見る。"""
    channel = root.find("channel")
    if channel is not None and (found := channel.findall("item")):
        return found
    # RSS 1.0(RDF)。名前空間つきの `<item>` が root の直下に並ぶ
    return root.findall(f"{_RSS1}item")


def _image(item) -> str:
    """1 件に付いている絵の URL。**フィードが配っているものだけ**を読む
    (ページを取りに行くのは本文を取ることになるので、しない)。"""
    for tag in (f"{_MEDIA}thumbnail", f"{_MEDIA}content", "enclosure"):
        found = item.find(tag)
        if found is not None:
            url = (found.get("url") or "").strip()
            kind = (found.get("type") or "").strip()
            # enclosure は音声や動画にも使われる。型が読めないことも普通にある
            if url and (not kind or kind.startswith("image") or tag != "enclosure"):
                return url
    return _text(item, f"{_HATENA}imageurl")


def _rss(root) -> list[dict]:
    items = _items(root)
    if not items:
        return []
    return [
        {
            "title": _text(item, "title", f"{_RSS1}title"),
            "url": _text(item, "link", f"{_RSS1}link"),
            "summary": _text(
                item, "description", f"{_RSS1}description"
            )[:MAX_SUMMARY_CHARS],
            "at": _when(_text(item, "pubDate", "{http://purl.org/dc/elements/1.1/}date")),
            "image": _image(item),
            "subjects": _subjects(
                item, "category", "{http://purl.org/dc/elements/1.1/}subject"
            ),
        }
        for item in items
    ]


def _atom(root) -> list[dict]:
    out = []
    for entry in root.findall(f"{_ATOM}entry"):
        link = entry.find(f"{_ATOM}link")
        out.append({
            "title": _text(entry, f"{_ATOM}title"),
            "url": (link.get("href") if link is not None else "") or "",
            "summary": _text(entry, f"{_ATOM}summary", f"{_ATOM}content")[:MAX_SUMMARY_CHARS],
            "at": _when(_text(entry, f"{_ATOM}updated", f"{_ATOM}published")),
            "image": _image(entry),
            # Atom は語を属性で持つ(`<category term="Codex"/>`)
            "subjects": _subjects(entry, f"{_ATOM}category"),
        })
    return out


def _when(raw: str) -> str:
    """日付を ISO に均す。**読めなければ空**(捨てずに、日付なしとして残す)。

    RSS は RFC 822、Atom は ISO 8601 と形が違い、しかもどちらにも崩れたものが混ざる。
    """
    if not raw:
        return ""
    for parse in (parsedate_to_datetime, datetime.fromisoformat):
        try:
            value = parse(raw)
        except (TypeError, ValueError):
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat(timespec="seconds")
    return ""


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _after(at: str, cutoff: datetime) -> bool:
    """**日付を持たないものは落とさない。** 絞り込みで静かに 0 件にしないため。"""
    if not at:
        return True
    try:
        return datetime.fromisoformat(at) > cutoff
    except ValueError:
        return True


def to_items(result: dict) -> list[dict]:
    """取ってきた見出しを、集める層が読む形(items)にして返す。

    返す形は AI に書かせたときとまったく同じ(`title` / `body` / `tags` / `url`)で、
    後ろの工程から見れば誰が作ったものかは区別が付かない(`app/extract.py` と同じ)。

    **要約を配っていないフィードがある。** 本文は取りに行かない契約なので、
    そのときは「配られていない」とだけ書く —— 見出しだけの 1 件でも、URL と配信日が
    あれば読む側は辿れる(ここで捨てると、そのフィードは丸ごと入らない)。

    **配信日は運ぶ**(`at`)。機械が正確に取れるものの 1 つで、しかも
    「前回の要約から今回まで」を数えるのに要る。
    """
    out = []
    for entry in result.get("items") or []:
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        summary = (entry.get("summary") or "").strip()
        source = (entry.get("from") or "").strip()
        out.append({
            "title": title,
            "body": summary or f"{source}の配信。要約は配信されていません。",
            "tags": _tags_of(entry),
            "url": entry.get("url") or "",
            "at": entry.get("at") or "",
            # **フィードが配っている絵**(ページを取りに行くわけではない)
            "image": entry.get("image") or "",
            # **配信元が 1 件ごとに付けている語。** 文書のタグには混ぜない ——
            # 束の名前と出典は「後から出典ごとに外す」ための印で、そこに記事ごとの
            # 語が混ざると、外す鍵として使えなくなる。読む側(AI)には `{feed}` で渡す
            "subjects": entry.get("subjects") or [],
        })
    return out


def _tags_of(entry: dict) -> list[str]:
    """その 1 件に付けるタグ —— 束の名前(指定に書いたもの)と配信元。

    **配信元は必ず入れる。** 同じ束に何本ものフィードが混ざるので、
    どこから来たのかがタグに無いと、後から出典ごとに外すことができない。
    """
    tags = list(entry.get("tags") or [])
    if source := (entry.get("from") or "").strip():
        tags.append(source)
    seen, out = set(), []
    for tag in tags:
        if tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def render(result: dict) -> str:
    """`{feed}` に差し込む文。

    **「参考であって、これが全部ではない」と明示する。** 道具が取ってきたものを
    情報源として扱わせると、フィードが拾わなかったものは永遠に入らないし、
    フィードが拾った宣伝記事はそのまま溜まる。**採否を決めるのは AI**。
    """
    items = result.get("items") or []
    head = (
        "道具が集めてきた見出し(**参考です。これが全部ではありません**)。\n"
        "**自分でも調べてください。** ここに無いものを足してよいし、"
        "ここにあっても要らないと判断したものは入れなくてよい。"
    )
    if failed := result.get("failed"):
        # 黙って減らさない —— 少ないのが世の中の都合か、道具の不調かで意味が違う
        head += f"\n※ {result.get('tried')} 件の出典のうち {failed} 件は取れませんでした。"
    if not items:
        return head + "\n(今回は 1 件も取れませんでした)"
    lines = "\n".join(
        f"- [{e['from']}] {e['title']}"
        + (f" — {e['summary']}" if e["summary"] else "")
        # **書き手が付けた語はそのまま渡す。** 製品名や技術名はここにしか出て
        # こないことがあり(見出しが「これ本当に APEX?」のような書き方のとき)、
        # 捨てると AI は推し量った分野しか書けない
        + (f" [語: {'、'.join(e['subjects'])}]" if e.get("subjects") else "")
        + (f" ({e['url']})" if e["url"] else "")
        for e in items if e["title"]
    )
    return f"{head}\n\n{lines}"
