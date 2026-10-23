"""サムネイル —— 1 件の絵を 1 回だけ取ってきて、小さく縮めて手元に持つ。

## 何のためか

収集の 1 件には絵の URL(`extra.image`)が付くことがある。読む側の画面がその URL を
そのまま指すと、**画面を開くたびに、見る人のブラウザが知らない外のサイトへつなぎに行く**。
ここで 1 回だけ取ってきて縮め、Chiezo から配る(`extra.thumb`)。読む側は Chiezo の
中だけを見ればよい。

## どの 1 件に作るか

**有効にした収集だけ**(`Collection.thumbs`)。そのうえで次のどちらか。

- **AI が返した絵の URL**(`image`)。AI はそのページを見て書いているので、
  指している先は AI が当たった公式サイトの絵になる
- **名指しした相手のページの `og:image`**(`thumbs.pages_from`)。配信に絵を載せない
  相手(告知サイトなど)のためのもので、**名指ししたホストのページしか読まない**

**配信(機械収集)が運んできた絵は縮めない。** 配信元が自分で配っている絵なので、
読む側がそのまま指せばよい —— ここで持つのは、AI が見つけた絵と、ページから拾った絵だけ。

## 1 件につき 1 回

作ったら `extra.thumb`、作れなかったら `extra.thumb_failed`(理由)を書く。
**どちらかが付いた 1 件には二度と取りに行かない**。脇書きは焼き直しても残るので
(`collect._merge_extra`)、次の回も印が見える。**つながらなかった・相手が 5xx か
429(多すぎる)を返したときだけは印を付けない**(一時的なものなので、次の回に取り直す)。
429 を「二度と取りに行かない」にしていた頃は、Wikimedia に続けて断られた 1 件が
絵を持たないまま残った(料理の収集で 18 件中 5 件)。その印の付いた 1 件も取り直す
(`RETRYABLE_FAILURES`)。

## 取れているかを控える

外へ取りに行った結果を 1 つの控えに残す(`health` / 設定の置き場の `thumbs_health`)。
取れた時刻・取れなかった時刻と理由・続けて取れなかった回数。**一時的な失敗は 1 件ごとの
印に残らない**ので、相手に断られ続けても、どの 1 件を見ても「まだ作っていない」としか
読めなかった(Wikimedia に 429 で断られ続けたのに、どこにも出ていなかった)。
状況の面(`/admin/status`)と `GET /v1/thumbs/health` から読む。**相手に届いたうえでの
404 などは数えない**(その 1 枚の事情で、取りに行けているかとは別)。

## 依頼の控え

1 枚ごとの結果を、新しいほうから `MAX_LOG` 件まで控える(`recent_log` / 設定の置き場の
`thumbs_log`)。いつ・誰の頼みで(写真・表紙の依頼か、どの収集の回か)・どの絵を・どうなったか
(取った・写しを返した・無い・取れない・休み中)。**取れているかの控え(`health`)は最後の 1 回と
回数だけ**なので、頼みが届いているのか、届いて何が返っているのかが読めなかった。
状況の面に畳んで出す。

## 断られたら休む

**「多すぎる」(429)と言われたら、その相手へはしばらく頼まない**(`RESTING_SECONDS` か、
相手の `Retry-After` の長いほう。`resting_until`)。休んでいる間の頼みは外へ出ずに断り
(`Resting`)、収集の中で取っている回はその場で残りをやめる。断られているのに 1 回の
巡回で 20 枚まで頼み続け、読む側のアプリからの頼みも 1.5 秒おきに通していた ——
Wikimedia は作り置きの無い縮小版を作らせる頼みを回線ごとに数えるので、頼むほど長く絞られる。

## Wikimedia の絵は縮小版を取る

`upload.wikimedia.org` の元の絵(数 MB のことがある)は取らず、Wikimedia が用意する
縮小版(`/thumb/…/<幅>px-<名前>`。`wikimedia_thumb`。幅は記事の代表の写真のプレビューの 330px)を
取る —— 縮めるのは手元なので
元の大きさは要らず、元の絵を続けて取ると 429 で断られやすい。縮小版が取れなければ
(元の絵が小さいと断られる)元の絵を取る。置き場の名前は元の URL から作る(同じ絵は 1 度)。

## Wikipedia のオフライン版にある絵は外へ出ない

外へ取りに行く前に、置いてある Wikipedia のオフライン版(Kiwix の ZIM。`app/wiki_zim.py`)を
引く。Wikimedia は作り置きの無い縮小版を作らせる頼みを回線ごとに数えて絞るので、1 枚ずつ
取りに行くと断られて休むたびに埋まるのが止まり、写真が何日も埋まらなかった。**オフライン版を
置いていれば、そこに無い Wikimedia の絵も外へ取りに行かずに諦める**(`NotOffline`。置くのは
Wikimedia に頼まないためで、無い 1 枚のために頼むと 429 で絞られる)。置いていなければ今までどおり
取りに行く。Wikimedia でない絵(表紙など)はオフライン版と関係なく取りに行く。
どちらから取ったかは控えの結果(`HOW_OFFLINE` / `HOW_FETCHED`)と
`POST /v1/thumbs` の `from` で分かる(外へ出なかった頼みなら、頼む側は間を空けずに次を頼める)。

## 外へ出る以上は守る(`app/feeds.py` と同じ契約)

- **名乗りはプロジェクト名だけ**(`feeds.USER_AGENT`)
- **自分でレート制限をかける**(`MIN_INTERVAL`)。1 回に作る数にも上限を置く
  (`MAX_PER_RUN`。残りは次の回に回る)
- **手元のネットワークを指す URL は取りに行かない**(`_public_host`)。URL は AI や
  外のページから来るので、そのまま叩くと LAN の中の機器を叩かされうる。
  転送も 1 段ずつ確かめる(自動では追わない)
- **絵だけを受け取る**(`Content-Type` が image/*、大きさに上限)。ページを読むのは
  名指しした相手だけで、読むのも頭の部分まで(`MAX_PAGE_BYTES`)
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import io
import json
import logging
import os
import re
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import HTTPException

from app import feeds, image_memory, notes, outbound, settings_store, wiki_zim

log = logging.getLogger("chiezo.app")

# 相手への礼儀としての最小間隔(秒)。`app/feeds.py` と同じ値
MIN_INTERVAL = 1.0
TIMEOUT = 15.0
# 1 回の収集で作る数の上限。**残りは次の回に回る**(配信の回は毎時走るので、
# 溜まっていたぶんも数回で片付く)。上げると、その回の取り込みが待たされる
MAX_PER_RUN = 20
# そのうえで、今回返ってこなかった既にある 1 件に後から作る数の上限(`backfill`)
MAX_BACKFILL_PER_RUN = 10
# 後から作る相手を探すとき、新しい順に見る数。ホストの合わない 1 件も混ざるので、
# 作る上限より多めに見る
BACKFILL_LOOKUP = 200
# 受け取る絵の大きさの上限(バイト)。キービジュアルでも数 MB に収まる
MAX_IMAGE_BYTES = 8 * 1024 * 1024
# ページは頭だけ読む(`og:image` は `<head>` にある)
MAX_PAGE_BYTES = 512 * 1024
# 縮めた長辺(px)。一覧の札に並べる大きさの 2 倍(高密度の画面でもにじまない)
SIZE = 320
QUALITY = 80
# 展開すると巨大になる絵(圧縮爆弾)を開かない。4000 万画素を超えるものは断る
MAX_PIXELS = 40_000_000
# 転送を追う段数
MAX_REDIRECTS = 3
# 取れているかの控え(設定の置き場のフラグ。`--workers 2` なのでプロセスの中には持たない)
HEALTH_FLAG = "thumbs_health"
# 続けてこの回数取れなければ「取れていない」とみなして目立たせる
FAILING_AFTER = 3
# 依頼の控え(設定の置き場のフラグ)と、残す件数
LOG_FLAG = "thumbs_log"
MAX_LOG = 200
# 「多すぎる」と言われたら、その相手へ頼まない長さ(秒。相手の Retry-After が長ければそちら)
RESTING_SECONDS = 30 * 60
# 取り直す失敗(一時的なものなのに、前は印を付けていた)
RETRYABLE_FAILURES = ("HTTP 429",)
# Wikimedia の縮小版の幅。**作り置きのありそうな幅**(記事の代表の写真をプレビューや検索で
# 出すときの幅)—— Wikimedia は作り置きの無い縮小版を作らせる頼みを回線ごとに数えて絞る
# (続けて頼むと 429)。試しに 330px を 12 枚続けて頼むと全部取れ、250px は 3 枚目から断られた
WIKIMEDIA_WIDTH = 330
_WIKIMEDIA_ORIGINAL = re.compile(
    r"^https://upload\.wikimedia\.org/wikipedia/([a-z-]+)/([0-9a-f])/([0-9a-f]{2})/"
    r"([^/?#]+\.(?:jpe?g|png|gif|webp))$",
    re.I,
)

# どうやって作ったか(依頼の控えの結果の言葉。`make` も返す)
HOW_CACHED = "写しを返した"
HOW_OFFLINE = "オフライン版から取った"
HOW_FETCHED = "取った"

# 置いたものの名前(sha1 の 16 進 + .webp)。配るときもこの形しか通さない
_NAME = re.compile(r"[0-9a-f]{40}\.webp")
_OG_IMAGE = re.compile(
    r"""<meta\s[^>]*?(?:property|name)\s*=\s*["']og:image(?::url)?["'][^>]*>""", re.I
)
_CONTENT = re.compile(r"""content\s*=\s*["']([^"']+)["']""", re.I)

_lock = asyncio.Lock()
_last_call = 0.0


class Resting(httpx.HTTPError):
    """その相手は断られて休んでいる(外へは出ていない)。一時的なものとして扱う。"""


class Skip(Exception):
    """その 1 件には作らない(理由つき)。**印を付けて、二度と取りに行かない**。"""


NOT_OFFLINE = "Wikipedia のオフライン版に無い"


class NotOffline(Skip):
    """Wikipedia のオフライン版を置いているのに、その絵が入っていなかった。**Wikimedia へは取りに行かない**。

    オフライン版を置くのは Wikimedia に頼まないためなので、無い絵は諦めて次へ進む。
    **外の絵の置き場(`image_memory`)には覚えるが、確かめ直す日まで断るのには使わない**
    (`offline` の印。新しい版を置けばその場で引ける。オフライン版を引くのは手元で、外へは出ない)。
    """


def thumbs_dir() -> Path | None:
    """置き場。状態ディレクトリの下(生成物の置き場とは分ける —— あちらは日が経つと
    掃除されるが、こちらは収集の 1 件が居るあいだずっと要る)。"""
    state = settings_store.state_dir()
    return state / "thumbs" if state else None


def public_path(name: str) -> str:
    """読む側へ渡す場所(Chiezo の中の相対パス)。"""
    return f"/v1/thumbs/{name}"


def resolve(name: str) -> Path:
    """配るためにパスを解く。**置いた形の名前しか通さない**(`../` を踏ませない)。"""
    root = thumbs_dir()
    if root is None or not _NAME.fullmatch(name or ""):
        raise HTTPException(404, {"error": "not found"})
    # **組んだ結果でも置き場の中かを確かめる**(`handoff._inside` と同じ形)。名前の形で
    # 外へは出ないが、形の決まりを変えたときに黙って外を配らないように
    base = os.path.realpath(root)
    full = os.path.realpath(os.path.join(base, name))
    if not full.startswith(base + os.sep) or not os.path.isfile(full):
        raise HTTPException(404, {"error": "not found"})
    return Path(full)


def normalize(raw) -> dict | None:
    """収集の設定の形を揃える。**無し(None)なら作らない**。

    `true` / `{}` なら AI が返した絵だけ、`{"pages_from": ["connpass.com"]}` なら
    そのホスト(とその下のサブドメイン)のページの `og:image` も拾う。`["*"]` なら
    どのホストのページも読む。
    """
    if raw in (None, False, ""):
        return None
    if raw is True:
        return {"pages_from": []}
    if not isinstance(raw, dict):
        raise HTTPException(400, {"error": "thumbs は true か {\"pages_from\": [ホスト]} で書いてください"})
    hosts = raw.get("pages_from") or []
    if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
        raise HTTPException(400, {"error": "thumbs.pages_from はホスト名の並びで書いてください"})
    cleaned = sorted({h.strip().lower().lstrip(".") for h in hosts if h.strip()})
    # `"*"` は「どのホストも」(`ANY_HOST`)。ほかのホストと並べても意味は同じ
    return {"pages_from": cleaned}


def _host_matches(url: str, hosts: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in hosts)


# `pages_from` に書くと、**どのホストのページも読む**印
ANY_HOST = "*"


def wants(spec: dict | None, extra: dict, image: str, url: str, from_feed: bool) -> str:
    """その 1 件で、何を元に作るか。`"image"`(絵の URL)/ `"page"`(ページ)/ 空。

    `pages_from` が `"*"` なら、ホストを問わずページの `og:image` を読む —— 1 件が
    公式サイトを指す収集(作品・イベント)のための口。**ただし配信が絵を運んできた
    1 件は読まない**(配信元が自分で配っている絵があるので、ページまで取りに行かない)。
    """
    failed = extra.get("thumb_failed")
    if spec is None or extra.get("thumb") or (failed and failed not in RETRYABLE_FAILURES):
        return ""
    if image and not from_feed:
        return "image"
    hosts = spec.get("pages_from") or []
    if url and _host_matches(url, [h for h in hosts if h != ANY_HOST]):
        return "page"
    if url and ANY_HOST in hosts and not (from_feed and image):
        return "page"
    return ""


async def _throttle() -> None:
    global _last_call
    async with _lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = time.monotonic()


async def _public_host(url: str) -> None:
    """**外のホストだけを通す**(`outbound.ensure_public`。断る理由はそのまま Skip にする)。"""
    try:
        await outbound.ensure_public(url)
    except outbound.Refused as e:
        raise Skip(str(e)) from e


def _client() -> httpx.AsyncClient:
    """外へ出るクライアント(テストはここを差し替える)。**転送は自分で追う**。"""
    return httpx.AsyncClient(
        timeout=TIMEOUT, headers={"User-Agent": feeds.USER_AGENT}, follow_redirects=False,
    )


async def _get(client: httpx.AsyncClient, url: str, limit: int) -> tuple[str, bytes, str]:
    """1 回取る(転送は 1 段ずつ確かめて追う)。`(最後の URL, 中身, Content-Type)`。

    上限を超えたら断る。**つながらない・5xx は例外のまま投げる**(印を付けない)。
    取れたか・取れなかったかは控えに残す(`_note`)。**休んでいる相手へは出ない**(`Resting`)。
    """
    host = urlparse(url).hostname or ""
    if until := resting_until(host):
        raise Resting(f"断られたので {until} まで頼みません")
    try:
        got = await outbound.fetch(
            client, url, limit, max_redirects=MAX_REDIRECTS,
            # ページは頭だけあればよい(`og:image` は `<head>` にある)
            truncate=limit == MAX_PAGE_BYTES, before_request=_throttle,
        )
    except httpx.HTTPError as e:
        _note(False, f"つながらない({type(e).__name__})", url)
        raise
    except outbound.TooLarge as e:
        raise Skip("大きすぎる") from e
    except outbound.TooManyRedirects as e:
        raise Skip("転送が多すぎる") from e
    except outbound.Refused as e:
        raise Skip(str(e)) from e
    # 多すぎる(429)も一時的なもの。印を付けずに次の回に取り直す
    if got.status >= 500 or got.status == 429:
        rest = _retry_after(got.response) if got.status == 429 else 0
        _note(False, f"HTTP {got.status}", url, rest=rest)
        raise httpx.HTTPStatusError(
            f"HTTP {got.status}", request=got.response.request, response=got.response
        )
    if got.status != 200:
        raise Skip(f"HTTP {got.status}")
    _note(True)
    return got.url, got.body, got.content_type


def health() -> dict:
    """取れているかの控え。`last_ok_at` / `last_failed_at`(UTC の ISO)・`last_reason`・
    `last_host`・`failing`(続けて取れなかった回数)。控えが無ければ空。"""
    try:
        raw = settings_store.get_flag(HEALTH_FLAG)
        state = json.loads(raw) if raw else {}
    # 控えが読めなくても絵を作るのは止めない(壊れた控えは無いものとして書き直す)
    except (OSError, ValueError, sqlite3.Error):
        log.warning("thumbs: 取れているかの控えを読めなかった", exc_info=True)
        return {}
    return state if isinstance(state, dict) else {}


def resting_until(host: str, state: dict | None = None) -> str:
    """その相手へ頼まない期限(UTC の ISO)。休んでいなければ空。"""
    state = health() if state is None else state
    until = str((state.get("resting") or {}).get(host) or "")
    try:
        if until and datetime.fromisoformat(until) > datetime.now(UTC):
            return until
    except ValueError:
        pass
    return ""


def _retry_after(response) -> int:
    """休む長さ(秒)。相手の `Retry-After`(秒)と `RESTING_SECONDS` の長いほう。"""
    try:
        asked = int(response.headers.get("retry-after") or 0)
    except (TypeError, ValueError):
        asked = 0
    return max(asked, RESTING_SECONDS)


def failing(state: dict | None = None) -> bool:
    """続けて取れていないか(`FAILING_AFTER` 回以上)。"""
    state = health() if state is None else state
    return int(state.get("failing") or 0) >= FAILING_AFTER


def _note(ok: bool, reason: str = "", url: str = "", rest: int = 0) -> None:
    """1 回ぶんの結果を控える。**控えに失敗しても絵を作るのは止めない**。"""
    if settings_store.db_path() is None:
        return
    try:
        state = health()
        now = datetime.now(UTC).isoformat(timespec="seconds")
        if ok:
            state.update(last_ok_at=now, failing=0)
        else:
            host = urlparse(url).hostname or ""
            state.update(
                last_failed_at=now,
                last_reason=reason,
                last_host=host,
                failing=int(state.get("failing") or 0) + 1,
            )
            if rest and host:
                until = datetime.now(UTC) + timedelta(seconds=rest)
                state["resting"] = {
                    **(state.get("resting") or {}),
                    host: until.isoformat(timespec="seconds"),
                }
        settings_store.set_flag(HEALTH_FLAG, json.dumps(state, ensure_ascii=False))
    except (OSError, sqlite3.Error):
        log.warning("thumbs: 取れているかを控えられなかった", exc_info=True)



def wikimedia_thumb(url: str, width: int = WIKIMEDIA_WIDTH) -> str:
    """Wikimedia の元の絵の URL を、縮小版の URL にする。当てはまらなければ空。"""
    found = _WIKIMEDIA_ORIGINAL.match(url)
    if not found:
        return ""
    project, a, ab, name = found.groups()
    return (
        f"https://upload.wikimedia.org/wikipedia/{project}/thumb/{a}/{ab}/{name}/{width}px-{name}"
    )


async def _get_image(client: httpx.AsyncClient, image: str) -> tuple[str, bytes, str]:
    """絵を取る。Wikimedia の元の絵は縮小版を先に取り、取れなければ元の絵を取る。"""
    thumb = wikimedia_thumb(image)
    if thumb:
        try:
            return await _get(client, thumb, MAX_IMAGE_BYTES)
        except Skip:
            # 元の絵が縮小版の幅より小さいと断られる(元の絵なら取れる)
            pass
    return await _get(client, image, MAX_IMAGE_BYTES)


def page_image_url(page: str, base: str) -> str:
    """ページの `og:image`。無ければ空。相対パスはページの URL から組み立てる。"""
    for tag in _OG_IMAGE.findall(page):
        if found := _CONTENT.search(tag):
            url = urljoin(base, html.unescape(found.group(1).strip()))
            if url.startswith(("http://", "https://")):
                return url
    return ""


def shrink(data: bytes) -> bytes:
    """縮めて WebP にする。**絵として開けなければ断る**。"""
    from PIL import Image  # 重いので使うときに読む

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.draft("RGB", (SIZE, SIZE))
            image.thumbnail((SIZE, SIZE))
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA" if "transparency" in image.info else "RGB")
            out = io.BytesIO()
            image.save(out, "WEBP", quality=QUALITY)
            return out.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        raise Skip(f"絵として開けない: {type(e).__name__}") from e


async def make(
    client: httpx.AsyncClient, kind: str, source: str, caller: str = "", remember: bool = True,
) -> tuple[str, str, str]:
    """1 件ぶん作る。`(配る場所, 元の絵の URL, どうしたか)`。作らない理由があれば `Skip`。
    どうしたかは控えの結果の言葉(`HOW_CACHED` / `HOW_OFFLINE` / `HOW_FETCHED`)。
    結果は依頼の控えに残す(`caller` は誰の頼みか。「写真の依頼」・収集の名前など)。"""
    try:
        thumb, image, how = await _make(client, kind, source, remember)
    except Skip as e:
        _log(caller, source, f"作れない({e})", ok=False)
        raise
    except Resting as e:
        _log(caller, source, f"休み中({e})", ok=False)
        raise
    except httpx.HTTPError as e:
        reason = str(e) if isinstance(e, httpx.HTTPStatusError) else type(e).__name__
        _log(caller, source, f"取れない({reason})", ok=False)
        raise
    _log(caller, source, how, ok=True)
    return thumb, image, how


def recent_log() -> list[dict]:
    """依頼の控え(新しい順)。`at`(UTC の ISO)・`caller`・`target`(絵の URL)・`result`・`ok`。"""
    try:
        raw = settings_store.get_flag(LOG_FLAG)
        entries = json.loads(raw) if raw else []
    except (OSError, ValueError, sqlite3.Error):
        log.warning("thumbs: 依頼の控えを読めなかった", exc_info=True)
        return []
    return entries if isinstance(entries, list) else []


def _log(caller: str, target: str, result: str, ok: bool) -> None:
    """1 枚ぶんの結果を控える。**控えに失敗しても絵を作るのは止めない**。"""
    if settings_store.db_path() is None:
        return
    entry = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "caller": caller or "不明",
        "target": target,
        "result": result,
        "ok": ok,
    }
    try:
        settings_store.set_flag(
            LOG_FLAG, json.dumps([entry, *recent_log()][:MAX_LOG], ensure_ascii=False)
        )
    except (OSError, sqlite3.Error):
        log.warning("thumbs: 依頼の控えを残せなかった", exc_info=True)


async def _make(
    client: httpx.AsyncClient, kind: str, source: str, remember: bool = True,
) -> tuple[str, str, str]:
    """`make` の中身。`(配る場所, 元の絵の URL, どうしたか)`。どうしたかは控えの結果の言葉
    (「写しを返した」・「オフライン版から取った」・「取った」)。

    **Wikipedia のオフライン版(`app/wiki_zim.py`)にある絵は外へ出ずに引く** —— Wikimedia は
    作り置きの無い縮小版を作らせる頼みを回線ごとに数えて絞るので、1 枚ずつ取りに行くと
    断られて休むたびに埋まるのが止まる。無い絵だけ外へ取りに行く。"""
    root = thumbs_dir()
    if root is None:
        raise Skip("置き場が無い")
    image = source
    if kind == "page":
        base, body, _type = await _get(client, source, MAX_PAGE_BYTES)
        image = page_image_url(body.decode("utf-8", "replace"), base)
        if not image:
            raise Skip("ページに og:image が無い")
    name = hashlib.sha1(image.encode("utf-8")).hexdigest() + ".webp"
    path = root / name
    # **同じ絵は 1 度だけ取る**(別の 1 件が同じ絵を指していることがある)
    if path.is_file():
        return public_path(name), image, HOW_CACHED
    # **前に確かめて無かった絵は、確かめ直す日まで外へ出ない**(`app/image_memory.py`)
    if missing := await asyncio.to_thread(image_memory.still_missing, image):
        raise Skip(f"前に確かめて無かった({missing['reason']})")
    how = HOW_OFFLINE
    data = await asyncio.to_thread(wiki_zim.image, image)
    # **オフライン版を置いていれば、Wikimedia の絵はそこに無くても外へ取りに行かない**
    # (置くのは Wikimedia に頼まないため。置いていなければ今までどおり取りに行く)
    if data is None and wiki_zim.covers(image) and await asyncio.to_thread(wiki_zim.placed):
        if remember:
            # 覚えるが、確かめ直す日まで断るのには使わない(新しい版を置けばその場で引ける)
            await asyncio.to_thread(image_memory.remember, image, reason=NOT_OFFLINE, offline=True)
        raise NotOffline(NOT_OFFLINE)
    try:
        if data is None:
            how = HOW_FETCHED
            _url, data, content_type = await _get_image(client, image)
            if not content_type.lower().startswith("image/"):
                raise Skip(f"絵ではない: {content_type or '型なし'}")
        small = await asyncio.to_thread(shrink, data)
    except Skip as e:
        # 取れないと分かった絵は覚える(一時的な失敗は `Skip` にならないので覚えない)
        if remember:
            await asyncio.to_thread(image_memory.remember, image, reason=str(e))
        raise
    root.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(small)
    tmp.replace(path)
    if remember:
        await asyncio.to_thread(image_memory.remember, image, thumb=public_path(name), how=how)
    return public_path(name), image, how


def _told_to_wait(error: Exception) -> bool:
    """「多すぎる」と言われた(か、休んでいる)か。**その回の残りは頼まない**。"""
    if isinstance(error, Resting):
        return True
    return isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 429


async def attach(
    spec: dict | None, items: list, existing: dict[str, dict], from_feed: bool, caller: str = "",
    remember: bool = True,
) -> int:
    """返ってきた 1 件ずつに、まだ無ければサムネイルを付ける(`extra.thumb`)。作った数を返す。

    `existing` は見出し → いま持っている脇書き(既にある 1 件の絵と印を見るため)。
    **その場で `items` を書き換える**(焼く前に脇書きとして載せる)。
    """
    if spec is None or thumbs_dir() is None:
        return 0
    made = 0
    tried = 0
    async with _client() as client:
        for raw in items:
            if tried >= MAX_PER_RUN:
                break
            if not isinstance(raw, dict) or notes.TOMBSTONE_TAG in (raw.get("tags") or []):
                continue
            extra_in = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
            have = existing.get(notes.title_key(raw.get("title")), {})
            known = {**have, **extra_in}
            image = str(raw.get("image") or known.get("image") or "").strip()
            url = str(raw.get("url") or known.get("url") or "").strip()
            kind = wants(spec, known, image, url, from_feed)
            if not kind:
                continue
            tried += 1
            try:
                thumb, found, _how = await make(
                    client, kind, image if kind == "image" else url, caller, remember
                )
            except Skip as e:
                raw["extra"] = {**extra_in, "thumb_failed": str(e)[:200]}
                continue
            except (httpx.HTTPError, OSError) as e:
                # **一時的なもの**。印を付けず、次の回に取り直す
                log.info("thumbs: 取れなかった(次の回に取り直す): %s: %s", url or image, e)
                if _told_to_wait(e):
                    break
                continue
            fresh = {"thumb": thumb}
            if kind == "page" and not known.get("image"):
                fresh["image"] = found
            raw["extra"] = {**extra_in, **fresh}
            made += 1
    return made


async def backfill(
    spec: dict | None, items: list, waiting: dict[str, dict], from_feed: bool, caller: str = "",
    remember: bool = True,
) -> int:
    """**今回返ってこなかった既にある 1 件**にも、まだ無ければサムネイルを付ける。

    `attach` が見るのは今回返ってきた 1 件だけなので、絵を持たないまま一度入った
    1 件は、AI が触り直さない限り絵が付かない(設定を広げても、育て終えた作品には
    届かない)。足すだけの回に限って、**脇書きだけを持つ 1 件**(見出しと
    `extra.thumb`)を `items` に足す —— 足すだけの回は既にある 1 件の中身に触らず、
    まだ持っていない脇書きだけを受け取るので、本文もタグも動かない。

    `waiting` は見出し → いま持っている脇書き(`collect.waiting_for_thumbs`)。
    1 回に作るのは `MAX_BACKFILL_PER_RUN` 件まで(残りは次の回)。作った数を返す。
    """
    if spec is None or thumbs_dir() is None or not waiting:
        return 0
    returned = {notes.title_key(raw.get("title")) for raw in items if isinstance(raw, dict)}
    made = 0
    tried = 0
    async with _client() as client:
        for title, extra in waiting.items():
            if tried >= MAX_BACKFILL_PER_RUN:
                break
            if title in returned:
                continue
            image = str(extra.get("image") or "").strip()
            url = str(extra.get("url") or "").strip()
            kind = wants(spec, extra, image, url, from_feed)
            if not kind:
                continue
            tried += 1
            try:
                thumb, found, _how = await make(
                    client, kind, image if kind == "image" else url, caller, remember
                )
            except Skip as e:
                items.append({"title": title, "extra": {"thumb_failed": str(e)[:200]}})
                continue
            except (httpx.HTTPError, OSError) as e:
                log.info("thumbs: 取れなかった(次の回に取り直す): %s: %s", url or image, e)
                if _told_to_wait(e):
                    break
                continue
            fresh = {"thumb": thumb}
            if kind == "page" and not image:
                fresh["image"] = found
            items.append({"title": title, "extra": fresh})
            made += 1
    return made
