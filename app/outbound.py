"""外の URL を取りに行くときの共通の関門(手元のネットワークへ向けさせない)。

URL は AI・外のページ・収集の定義・頼む側の引数から来る。そのまま叩くと、
**LAN の中の機器(ルーター・NAS・取り込みのトリガー)を Chiezo に叩かせる**ことが
できてしまう(SSRF)。最初の URL が外を指していても、転送先が手元を指すことがあるので、
**転送は自動で追わず、1 段ごとに名前を引いて確かめる**。

もう 1 つは大きさ。`response.content` で全部読んでから測ると、上限を超える大きさを
いったんメモリに抱える。**読みながら数えて、上限で止める**。

もとは `app/thumbs.py` が自前で持っていた検査で、フィード・文字起こし・絵の元にも
同じものが要るのでここへ出した。
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

# 転送を追う段数の既定
MAX_REDIRECTS = 3


class Refused(Exception):
    """取りに行かない(理由つき)。文言はそのまま人に見せてよい形にしてある。"""


class NotPublic(Refused):
    """手元・LAN・予約済みのアドレスを指している。"""


class TooLarge(Refused):
    """上限を超えた。"""


class TooManyRedirects(Refused):
    """転送が多すぎる。"""


@dataclass
class Fetched:
    url: str
    status: int
    content_type: str
    body: bytes
    response: httpx.Response


async def ensure_public(url: str) -> None:
    """**外のホストだけを通す。** 名前を引いて、手元・LAN・予約済みのアドレスなら断る。

    引いた結果のどれか 1 つでも手元なら断る(複数のアドレスを返す名前で、
    接続のときに手元のほうを選ばれうるため)。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise Refused("http(s) の URL ではない")
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, None)
    except socket.gaierror as e:
        raise Refused(f"名前を引けない: {parsed.hostname}") from e
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise NotPublic(f"手元のネットワークを指している: {parsed.hostname}")


async def fetch(
    client: httpx.AsyncClient,
    url: str,
    limit: int,
    *,
    max_redirects: int = MAX_REDIRECTS,
    truncate: bool = False,
    before_request: Callable[[], Awaitable[None]] | None = None,
) -> Fetched:
    """1 回取る。転送は 1 段ずつ確かめて追い、本文は `limit` バイトで止める。

    `client` は `follow_redirects=False` で作ったものを渡すこと(自動で追われると、
    途中の段を確かめられない)。`truncate=True` なら上限で切って返し(ページの頭だけ
    あればよいとき)、そうでなければ `TooLarge` を投げる。
    状態コードの扱いは呼ぶ側が決める(転送以外はそのまま返す)。
    """
    for _ in range(max_redirects + 1):
        await ensure_public(url)
        if before_request is not None:
            await before_request()
        async with client.stream("GET", url) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers.get("location", ""))
                continue
            body = bytearray()
            if response.status_code < 400:
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > limit:
                        if truncate:
                            del body[limit:]
                            break
                        raise TooLarge(f"大きすぎる(上限 {limit:,} バイト)")
            return Fetched(
                url=url,
                status=response.status_code,
                content_type=response.headers.get("content-type", ""),
                body=bytes(body),
                response=response,
            )
    raise TooManyRedirects("転送が多すぎる")
