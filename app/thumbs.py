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

## Wikimedia の絵は縮小版を取る

`upload.wikimedia.org` の元の絵(数 MB のことがある)は取らず、Wikimedia が用意する
縮小版(`/thumb/…/<幅>px-<名前>`。`wikimedia_thumb`)を取る —— 縮めるのは手元なので
元の大きさは要らず、元の絵を続けて取ると 429 で断られやすい。縮小版が取れなければ
(元の絵が小さいと断られる)元の絵を取る。置き場の名前は元の URL から作る(同じ絵は 1 度)。

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
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import HTTPException

from app import feeds, notes, outbound, settings_store

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
# 取り直す失敗(一時的なものなのに、前は印を付けていた)
RETRYABLE_FAILURES = ("HTTP 429",)
# Wikimedia の縮小版の幅。Wikimedia が用意している幅のうち、縮めた長辺(`SIZE`)より大きいもの
WIKIMEDIA_WIDTH = 500
_WIKIMEDIA_ORIGINAL = re.compile(
    r"^https://upload\.wikimedia\.org/wikipedia/([a-z-]+)/([0-9a-f])/([0-9a-f]{2})/"
    r"([^/?#]+\.(?:jpe?g|png|gif|webp))$",
    re.I,
)

# 置いたものの名前(sha1 の 16 進 + .webp)。配るときもこの形しか通さない
_NAME = re.compile(r"[0-9a-f]{40}\.webp")
_OG_IMAGE = re.compile(
    r"""<meta\s[^>]*?(?:property|name)\s*=\s*["']og:image(?::url)?["'][^>]*>""", re.I
)
_CONTENT = re.compile(r"""content\s*=\s*["']([^"']+)["']""", re.I)

_lock = asyncio.Lock()
_last_call = 0.0


class Skip(Exception):
    """その 1 件には作らない(理由つき)。**印を付けて、二度と取りに行かない**。"""


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
    """
    try:
        got = await outbound.fetch(
            client, url, limit, max_redirects=MAX_REDIRECTS,
            # ページは頭だけあればよい(`og:image` は `<head>` にある)
            truncate=limit == MAX_PAGE_BYTES, before_request=_throttle,
        )
    except outbound.TooLarge as e:
        raise Skip("大きすぎる") from e
    except outbound.TooManyRedirects as e:
        raise Skip("転送が多すぎる") from e
    except outbound.Refused as e:
        raise Skip(str(e)) from e
    # 多すぎる(429)も一時的なもの。印を付けずに次の回に取り直す
    if got.status >= 500 or got.status == 429:
        raise httpx.HTTPStatusError(
            f"{got.status}", request=got.response.request, response=got.response
        )
    if got.status != 200:
        raise Skip(f"HTTP {got.status}")
    return got.url, got.body, got.content_type


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


async def make(client: httpx.AsyncClient, kind: str, source: str) -> tuple[str, str]:
    """1 件ぶん作る。`(配る場所, 元の絵の URL)`。作らない理由があれば `Skip`。"""
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
    if not path.is_file():
        _url, data, content_type = await _get_image(client, image)
        if not content_type.lower().startswith("image/"):
            raise Skip(f"絵ではない: {content_type or '型なし'}")
        small = await asyncio.to_thread(shrink, data)
        root.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(small)
        tmp.replace(path)
    return public_path(name), image


async def attach(spec: dict | None, items: list, existing: dict[str, dict], from_feed: bool) -> int:
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
                thumb, found = await make(client, kind, image if kind == "image" else url)
            except Skip as e:
                raw["extra"] = {**extra_in, "thumb_failed": str(e)[:200]}
                continue
            except (httpx.HTTPError, OSError) as e:
                # **一時的なもの**。印を付けず、次の回に取り直す
                log.info("thumbs: 取れなかった(次の回に取り直す): %s: %s", url or image, e)
                continue
            fresh = {"thumb": thumb}
            if kind == "page" and not known.get("image"):
                fresh["image"] = found
            raw["extra"] = {**extra_in, **fresh}
            made += 1
    return made


async def backfill(
    spec: dict | None, items: list, waiting: dict[str, dict], from_feed: bool,
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
                thumb, found = await make(client, kind, image if kind == "image" else url)
            except Skip as e:
                items.append({"title": title, "extra": {"thumb_failed": str(e)[:200]}})
                continue
            except (httpx.HTTPError, OSError) as e:
                log.info("thumbs: 取れなかった(次の回に取り直す): %s: %s", url or image, e)
                continue
            fresh = {"thumb": thumb}
            if kind == "page" and not image:
                fresh["image"] = found
            items.append({"title": title, "extra": fresh})
            made += 1
    return made
