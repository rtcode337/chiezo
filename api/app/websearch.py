"""web 検索の道具 — ためた知識で足りないぶんを外から補う(既定では無効)。

**これは「使う」層(= Chiezo を使う側)の機能であって、Chiezo 本体の機能ではない**。
Chiezo は AI のための知識ベースで、その AI をローカル LLM で同居させたのが「使う」層。
Claude Code が Chiezo と web 検索の両方を持っているのと同じ関係で、
**知識ベースそのものは今までどおり外を一切叩かない**(ingest がダンプを取る以外)。
だから存在理由と矛盾はしないが、外へ出る以上は次を守る:

- **どれが web 由来かを必ず出す**。出典(`references`)の `source` が `web` になり、URL が付く。
  Chiezo の文書と混ざったまま「どこから来た話か分からない」状態にしない
- **自分でレート制限をかける**(`MIN_INTERVAL`)。ツールループはモデルの気分で何度でも呼ぶので、
  呼ばれた回数ぶん素直に外へ出さない
- **`User-Agent` に個人情報を入れない**。名乗るのはプロジェクト名だけで、
  git の設定や環境変数からメールアドレスを拾ってはいけない
- **本文は取りに行かない**。返すのは検索結果のタイトル・要約・URL だけ。ページを取得して
  中身を読むのはスクレイピングに踏み込む話で、相手への負担も壊れやすさも別次元になる

プロバイダは 2 つ。**自前で立てた SearXNG を第一に置いている**のは、Chiezo と同じ
「LAN 内で完結する」置き方ができるから(検索先は SearXNG が面倒を見る)。Brave は
公式 API があるので、鍵を持っているならそちらでもよい、という位置づけ。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

log = logging.getLogger("chiezo.api")

# 相手への礼儀としての最小間隔(秒)。ツールループは何度でも呼びうるので、ここで待たせる。
MIN_INTERVAL = 1.0
# 名乗り。プロジェクト名だけを入れる(連絡先・個人名・メールアドレスは入れない)。
USER_AGENT = "chiezo (local knowledge server)"

_last_call = 0.0
_lock = asyncio.Lock()


def is_enabled() -> bool:
    return bool(os.environ.get("CHIEZO_WEB_SEARCH_URL", "").strip())


def _provider() -> str:
    return os.environ.get("CHIEZO_WEB_SEARCH_PROVIDER", "searxng").strip().lower() or "searxng"


def _results_limit() -> int:
    raw = os.environ.get("CHIEZO_WEB_SEARCH_RESULTS", "").strip()
    try:
        return max(1, min(10, int(raw))) if raw else 5
    except ValueError:
        return 5


def _timeout() -> float:
    raw = os.environ.get("CHIEZO_WEB_SEARCH_TIMEOUT", "").strip()
    try:
        return float(raw) if raw else 10.0
    except ValueError:
        return 10.0


def _client() -> httpx.AsyncClient:
    """web 検索向けの HTTP クライアント(テストはここを差し替える)。"""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    key = os.environ.get("CHIEZO_WEB_SEARCH_API_KEY", "").strip()
    if key and _provider() == "brave":
        headers["X-Subscription-Token"] = key
    return httpx.AsyncClient(timeout=_timeout(), headers=headers, follow_redirects=True)


async def _throttle() -> None:
    """呼ばれた回数ぶん素直に外へ出さない(最小間隔を空ける)。"""
    global _last_call
    async with _lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = time.monotonic()


def _params(provider: str, q: str, limit: int) -> dict:
    if provider == "brave":
        return {"q": q, "count": limit}
    # SearXNG は JSON 出力を明示しないと HTML を返す
    return {"q": q, "format": "json", "language": "ja"}


def _parse(provider: str, data: dict, limit: int) -> list[dict]:
    if provider == "brave":
        rows = (data.get("web") or {}).get("results") or []
        keys = ("title", "description", "url")
    else:
        rows = data.get("results") or []
        keys = ("title", "content", "url")
    out = []
    for row in rows[:limit]:
        if not isinstance(row, dict) or not row.get(keys[2]):
            continue
        out.append({
            "title": str(row.get(keys[0]) or "")[:200],
            "snippet": str(row.get(keys[1]) or "")[:500],
            "url": str(row[keys[2]]),
        })
    return out


async def search(q: str, limit: int | None = None) -> dict:
    """web を検索し、タイトル・要約・URL を返す。

    失敗は例外にせず `{"error": ...}` で返す(agent ループの他の道具と同じ扱い。
    外の世界は落ちるものなので、落ちたら別の手に移れればよい)。
    """
    url = os.environ.get("CHIEZO_WEB_SEARCH_URL", "").strip()
    if not url:
        return {"error": "web search is disabled"}
    provider = _provider()
    limit = limit or _results_limit()
    await _throttle()
    try:
        async with _client() as client:
            res = await client.get(url, params=_params(provider, q, limit))
    except httpx.HTTPError as e:
        # 例外の文言(接続先のホスト名等が入る)はログにだけ残し、モデルには種別を返す
        log.info("web search failed: %r", e)
        return {"error": "web search unreachable", "reason": type(e).__name__}
    if res.status_code >= 400:
        log.info("web search error %s: %s", res.status_code, res.text[:200])
        return {"error": f"web search error {res.status_code}"}
    try:
        data = res.json()
    except ValueError:
        return {"error": "web search returned a non-JSON response"}
    results = _parse(provider, data, limit)
    return {"query": q, "provider": provider, "results": results}


# agent に渡す道具の定義(OpenAI の function 形式)。Chiezo の道具は MCP から借りるが、
# こちらは MCP に出していないので、ここで定義する。**MCP に出さないのは意図的**で、
# Chiezo の MCP は「ためた知識の引き口」であり、web 検索はその外側だから
# (MCP の利用者である Claude Code は自前の web 検索を持っている)。
TOOL_NAME = "web_search"

TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "web を検索する。**Chiezo(ローカルの知識)で足りないときだけ使う**"
            "(取り込んだダンプに無い最近の出来事、いま現在の状態など)。"
            "返るのはタイトル・要約・URL だけで、ページ本文は取得しない。"
            "検索できる回数には限りがあるので、まず Chiezo を引くこと。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "検索語"},
            },
            "required": ["q"],
        },
    },
}


def references_from(results: dict) -> list[dict]:
    """検索結果を出典の形にする(Chiezo の文書と混ざっても web だと分かるように)。"""
    return [
        {"source": "web", "title": r["title"] or r["url"], "doc_id": None, "url": r["url"]}
        for r in results.get("results", [])
    ]
