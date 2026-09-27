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
(`collect._merge_extra`)、次の回も印が見える。**つながらなかった・相手が 5xx を
返したときだけは印を付けない**(一時的なものなので、次の回に取り直す)。

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
import ipaddress
import logging
import re
import socket
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import HTTPException

from app import feeds, notes, settings_store

log = logging.getLogger("chiezo.app")

# 相手への礼儀としての最小間隔(秒)。`app/feeds.py` と同じ値
MIN_INTERVAL = 1.0
TIMEOUT = 15.0
# 1 回の収集で作る数の上限。**残りは次の回に回る**(配信の回は毎時走るので、
# 溜まっていたぶんも数回で片付く)。上げると、その回の取り込みが待たされる
MAX_PER_RUN = 20
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
    path = root / name
    if not path.is_file():
        raise HTTPException(404, {"error": "not found"})
    return path


def normalize(raw) -> dict | None:
    """収集の設定の形を揃える。**無し(None)なら作らない**。

    `true` / `{}` なら AI が返した絵だけ、`{"pages_from": ["connpass.com"]}` なら
    そのホスト(とその下のサブドメイン)のページの `og:image` も拾う。
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
    return {"pages_from": cleaned}


def _host_matches(url: str, hosts: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in hosts)


def wants(spec: dict | None, extra: dict, image: str, url: str, from_feed: bool) -> str:
    """その 1 件で、何を元に作るか。`"image"`(絵の URL)/ `"page"`(ページ)/ 空。"""
    if spec is None or extra.get("thumb") or extra.get("thumb_failed"):
        return ""
    if image and not from_feed:
        return "image"
    if url and _host_matches(url, spec.get("pages_from") or []):
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
    """**外のホストだけを通す。** 名前を引いて、手元・LAN・予約済みのアドレスなら断る。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise Skip("http(s) の URL ではない")
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, None)
    except socket.gaierror as e:
        raise Skip(f"名前を引けない: {parsed.hostname}") from e
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise Skip(f"手元のネットワークを指している: {parsed.hostname}")


def _client() -> httpx.AsyncClient:
    """外へ出るクライアント(テストはここを差し替える)。**転送は自分で追う**。"""
    return httpx.AsyncClient(
        timeout=TIMEOUT, headers={"User-Agent": feeds.USER_AGENT}, follow_redirects=False,
    )


async def _get(client: httpx.AsyncClient, url: str, limit: int) -> tuple[str, bytes, str]:
    """1 回取る(転送は 1 段ずつ確かめて追う)。`(最後の URL, 中身, Content-Type)`。

    上限を超えたら断る。**つながらない・5xx は例外のまま投げる**(印を付けない)。
    """
    for _ in range(MAX_REDIRECTS + 1):
        await _public_host(url)
        await _throttle()
        async with client.stream("GET", url) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers.get("location", ""))
                continue
            if response.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"{response.status_code}", request=response.request, response=response
                )
            if response.status_code != 200:
                raise Skip(f"HTTP {response.status_code}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > limit:
                    if limit == MAX_PAGE_BYTES:
                        # ページは頭だけあればよい
                        break
                    raise Skip("大きすぎる")
            return url, bytes(body), response.headers.get("content-type", "")
    raise Skip("転送が多すぎる")


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
        _url, data, content_type = await _get(client, image, MAX_IMAGE_BYTES)
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
