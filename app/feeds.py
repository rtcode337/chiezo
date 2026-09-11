"""外向きの道具 —— RSS / Atom を機械的に取ってきて、AI に**参考として**渡す。

## 何のためか

`app/extract.py` が手元の長期記憶から機械的に引く道具なら、こちらは外から引く道具。
どちらも狙いは同じで、**AI が間違えるところと、間違えないところを分ける**こと ——
見出しと URL と日付は機械が正確に取れる。何が重要でどう束ねるかは取れない。

## これは素材であって、情報源ではない

**取ってきたものをそのまま溜めない。** `{feed}` でプロンプトへ差し込むだけで、
何を DB へ入れるかは AI が決める —— 自分でも web を検索し、渡されたぶんも含めて
採否を判断する。だから道具が取りこぼしても穴にはならないし、道具が拾った宣伝記事が
そのまま溜まることもない。

**溜まるのは常に AI が書いたもの。** 生のまま入る経路は作っていない。

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
import logging
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx
from fastapi import HTTPException

log = logging.getLogger("chiezo.app")

# 相手への礼儀としての最小間隔(秒)。`app/websearch.py` と同じ値・同じ理由。
MIN_INTERVAL = 1.0
# 名乗り。プロジェクト名だけを入れる(連絡先・個人名・メールアドレスは入れない)。
USER_AGENT = "chiezo (local knowledge server)"
TIMEOUT = 15.0

# 1 つの収集が持てるフィードの数。**増やすほど 1 回の実行が遅くなる**
# (順に取るので、10 本で最短 10 秒)
MAX_URLS = 10
# 差し込む見出しの数(全フィード合わせて)。素材であって一覧ではないので、
# 多くしても AI の読む量が増えるだけ
MAX_ITEMS = 200
DEFAULT_LIMIT = 60
# 1 本ぶんに読む大きさ。**大きすぎるものは読まない** —— フィードは索引であって
# 全文の配布物ではないので、これを超えるものは相手の設定がおかしい
MAX_BYTES = 2 * 1024 * 1024
# 1 件の要約の長さ。長い本文を丸ごと載せる相手がいるので切る
MAX_SUMMARY_CHARS = 300

# 前回の実行より後のものだけを渡す、の印
SINCE_LAST_RUN = "last_run"

_ATOM = "{http://www.w3.org/2005/Atom}"

_last_call = 0.0
_lock = asyncio.Lock()


def _bad(message: str) -> HTTPException:
    return HTTPException(400, {"error": f"フィードの指定が読めません: {message}"})


def normalize(raw) -> dict | None:
    """指定を検証して、欠けている鍵を埋めた形にする。無ければ None。

    **読めない指定は黙って無視せず断る**(`app/extract.py` と同じ判断)。
    無視すると、道具を付けたつもりの収集が道具なしで回り続ける。
    """
    if raw in (None, "", {}):
        return None
    if not isinstance(raw, dict):
        raise _bad("オブジェクトで書いてください")
    urls = [str(u).strip() for u in (raw.get("urls") or []) if str(u).strip()]
    if not urls:
        raise _bad("urls に 1 本以上書いてください")
    if len(urls) > MAX_URLS:
        raise _bad(f"urls は {MAX_URLS} 本まで(いまは {len(urls)} 本)")
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise _bad(f"http(s) の URL を書いてください: {url}")
    try:
        limit = int(raw.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        raise _bad("limit は数で書いてください") from None
    return {
        "urls": urls,
        "limit": max(1, min(limit, MAX_ITEMS)),
        # 前回より後のものだけにするか。**既定は絞らない** —— 日付を持たない
        # フィードが普通にあり、絞ると静かに 0 件になる
        "since": SINCE_LAST_RUN if raw.get("since") == SINCE_LAST_RUN else None,
    }


def to_json(spec: dict | None) -> dict | None:
    """定義のメモへ書ける形。**空の鍵は落とす**(読むときに邪魔なだけ)。"""
    if not spec:
        return None
    return {k: v for k, v in spec.items() if v not in (None, "")}


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
    items: list[dict] = []
    failed = 0
    for url in spec["urls"]:
        await _throttle()
        try:
            entries = await _fetch_one(url)
        except (httpx.HTTPError, ElementTree.ParseError, ValueError) as e:
            # 例外の文言(接続先のホスト名等が入る)はログにだけ残す
            log.info("feed fetch failed %s: %r", url, e)
            failed += 1
            continue
        items += [e for e in entries if cutoff is None or _after(e["at"], cutoff)]
    # **新しい順に切る。** 何本のフィードから来たかに関わらず、読む側に効くのは新しさ
    items.sort(key=lambda e: e["at"] or "", reverse=True)
    return {"items": items[: spec["limit"]], "failed": failed, "tried": len(spec["urls"])}


async def _fetch_one(url: str) -> list[dict]:
    async with _client() as client:
        res = await client.get(url)
    res.raise_for_status()
    body = res.content[:MAX_BYTES]
    if _looks_unsafe(body):
        raise ValueError("DTD のある XML は読まない")
    root = ElementTree.fromstring(body)
    source = _source_name(root, url)
    entries = _rss(root) or _atom(root)
    return [{**e, "from": source} for e in entries]


def _looks_unsafe(body: bytes) -> bool:
    """DTD(`<!DOCTYPE`)を持っているか。

    実体参照の展開でメモリを食い尽くす細工に付き合わないための門前払い。
    **フィードに DTD が付くことは実際まず無い**ので、落とす副作用はほぼ無い。
    """
    return b"<!DOCTYPE" in body[:4096] or b"<!ENTITY" in body[:4096]


def _text(node, *paths: str) -> str:
    for path in paths:
        found = node.find(path)
        if found is not None and (found.text or "").strip():
            return " ".join((found.text or "").split())
    return ""


def _source_name(root, url: str) -> str:
    """出典の名前。取れなければホスト名(どこから来たかは必ず出す)。"""
    channel = root.find("channel")
    name = _text(channel, "title") if channel is not None else _text(root, f"{_ATOM}title")
    return name or urlparse(url).netloc


def _rss(root) -> list[dict]:
    channel = root.find("channel")
    if channel is None:
        return []
    return [
        {
            "title": _text(item, "title"),
            "url": _text(item, "link"),
            "summary": _text(item, "description")[:MAX_SUMMARY_CHARS],
            "at": _when(_text(item, "pubDate", "{http://purl.org/dc/elements/1.1/}date")),
        }
        for item in channel.findall("item")
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
        + (f" ({e['url']})" if e["url"] else "")
        for e in items if e["title"]
    )
    return f"{head}\n\n{lines}"
