"""URL のはてなブックマーク数(`GET /v1/bookmarks`)。

**読む側のアプリに外を叩かせないための口。** 記事の並べ替えに反応の実数を使うアプリが、
自分ではてなの API を引いていた —— 外への通信先が Chiezo のほかにも散らばり、
加減(間隔・名乗り)もアプリごとにばらばらになる。ここで引いて覚え、アプリは Chiezo
だけを見る。

決めごと:

- **1 回に 50 本ずつ**(はてなの `count/entries` が受け付ける上限)。それより多ければ分けて、
  あいだを空ける(`MIN_INTERVAL`)
- **1 時間覚える**(`TTL_SECONDS`)。数は 1 時間で大きくは動かないので、細かく引き直しても
  並びはほとんど変わらない。**覚えるのはプロセスの中だけ**(`--workers 2` なので、ワーカーごとに
  1 度ずつ引くことはある。数はいつでも引き直せるので、置き場は持たない)
- **引けなかった URL は返さない**(0 と書かない)—— 0 は「誰もブックマークしていない」で、
  引けなかったこととは意味が違う
- 名乗りは `feeds.USER_AGENT`(外へ出る道具と同じ)
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlencode

import httpx

from app import feeds

log = logging.getLogger("chiezo.bookmarks")

ENDPOINT = "https://bookmark.hatenaapis.com/count/entries"
BATCH = 50
# 1 回に受け付ける URL の上限(読む側の 1 画面ぶんで足りる。際限なく外を叩かせない)
MAX_URLS = 500
TTL_SECONDS = 60 * 60
MIN_INTERVAL = 1.0
TIMEOUT = 15.0

# URL → (数, 引いた時刻)
_cache: dict[str, tuple[int, float]] = {}
_lock = asyncio.Lock()
_last_call = 0.0


def _client() -> httpx.AsyncClient:
    """外へ出るクライアント(テストはここを差し替える)。"""
    return httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": feeds.USER_AGENT})


async def _throttle() -> None:
    global _last_call
    async with _lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = time.monotonic()


async def counts(urls: list[str]) -> dict[str, int]:
    """URL → はてなブックマーク数。覚えているものは引き直さない。引けなかった URL は入らない。"""
    wanted = list(dict.fromkeys(u for u in urls if u.startswith(("http://", "https://"))))
    now = time.monotonic()
    known = {u: _cache[u][0] for u in wanted if u in _cache and now - _cache[u][1] < TTL_SECONDS}
    missing = [u for u in wanted if u not in known]
    if not missing:
        return known
    fetched: dict[str, int] = {}
    async with _client() as client:
        for start in range(0, len(missing), BATCH):
            batch = missing[start : start + BATCH]
            await _throttle()
            try:
                response = await client.get(ENDPOINT + "?" + urlencode([("url", u) for u in batch]))
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, ValueError) as e:
                # **引けなかったぶんは数を持たないだけ**(読む側は数の無い記事として出す)
                log.info("bookmark counts failed: %s", type(e).__name__)
                continue
            if isinstance(body, dict):
                for url, count in body.items():
                    if url in batch and isinstance(count, int):
                        fetched[url] = count
    stamp = time.monotonic()
    for url, count in fetched.items():
        _cache[url] = (count, stamp)
    return {**known, **fetched}


def clear() -> None:
    """忘れる(テスト用)。"""
    _cache.clear()
