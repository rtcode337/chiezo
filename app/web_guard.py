"""ブラウザ越しの悪用を止める関門(CSRF と DNS リバインディング)。

Chiezo は認証を持たない LAN 内のサービスなので、**その LAN の端末で開いている
任意のページ**が、ブラウザを踏み台にして Chiezo を叩けてしまう。止めたいのは 2 つ:

- **CSRF** —— 外のページがフォームや fetch で POST を送り、管理画面の操作
  (鍵の登録・相手の on/off・記憶の消去…)を勝手に走らせる。
  ブラウザは状態を変える要求に `Origin`(と `Sec-Fetch-Site`)を付けるので、
  **自分のオリジン以外から来たものを断る**。curl・他のアプリのサーバー側の
  HTTP クライアント・MCP クライアントはどちらも付けないので、これまでどおり通る。
- **DNS リバインディング** —— 外の名前(`evil.example`)を途中から LAN のアドレスへ
  向け直し、ブラウザに「同じオリジン」と思わせて読み書きする。名前で来た要求の
  `Host` は `evil.example` のままなので、**知らない名前の Host を断る**。

Host の検査は、`CHIEZO_ALLOWED_HOSTS`(か `CHIEZO_MCP_ALLOWED_HOSTS`)を書いたときだけ効く。
既定は検査しない —— VPN 越しの名前や、後から付けた名前で開いたときに、気づかないうちに
締め出される方が困るため。書いたときの判定は次のとおり:

- IP アドレスそのもの(`192.0.2.10:7010`・`[::1]`)と `localhost` は通す
  (リバインディングは名前を使う攻撃なので、IP で来た要求は対象にならない)
- ドットの無い名前(`chiezo-app` などコンテナのサービス名)も通す
- それ以外の名前(`chiezo.example.com`)は、書いたものだけ
"""
from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

# 状態を変えるメソッド。読む口(GET・HEAD・OPTIONS)は CORS の側で扱う
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _split(raw: str) -> list[str]:
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def allowed_hosts() -> list[str]:
    """名前で受けてよい Host。`CHIEZO_ALLOWED_HOSTS` と `CHIEZO_MCP_ALLOWED_HOSTS` を合わせる。

    書き方は `名前`・`名前:ポート`・`名前:*`(ポート任意)・`*.例`(下の名前すべて)・`*`(検査しない)。
    """
    return _split(os.environ.get("CHIEZO_ALLOWED_HOSTS", "")) + _split(
        os.environ.get("CHIEZO_MCP_ALLOWED_HOSTS", "")
    )


def allowed_origins() -> list[str]:
    """自分以外で、状態を変える要求を送ってきてよいオリジン(`CHIEZO_ALLOWED_ORIGINS`)。"""
    return [o.rstrip("/") for o in _split(os.environ.get("CHIEZO_ALLOWED_ORIGINS", ""))]


def split_host(value: str) -> tuple[str, str]:
    """`Host` の値を (名前, ポート) に分ける。IPv6 の `[::1]:7010` も扱う。"""
    value = (value or "").strip().lower()
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return "", ""
        name, rest = value[1:end], value[end + 1:]
        return name, rest[1:] if rest.startswith(":") else ""
    if value.count(":") == 1:
        name, port = value.split(":", 1)
        return name, port
    # ":" が 2 つ以上なら角括弧なしの IPv6(Host としては不正だが、名前として扱う)
    return value, ""


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def host_allowed(value: str) -> bool:
    """この `Host` で来た要求を受けてよいか(既定の考え方はモジュールの説明)。"""
    name, port = split_host(value)
    if not name:
        return False
    entries = allowed_hosts()
    # 何も書いていなければ検査しない(既定。理由はモジュールの説明)
    if not entries or _is_ip(name) or name == "localhost" or "." not in name.rstrip("."):
        return True
    for entry in entries:
        if entry == "*":
            return True
        entry_name, entry_port = split_host(entry)
        # `*.例` は下の名前すべて(`例` そのものは含まない)
        matched = name.endswith(entry_name[1:]) if entry_name.startswith("*.") else name == entry_name
        if matched and entry_port in ("", "*", port):
            return True
    return False


def _default_port(scheme: str) -> str:
    return {"http": "80", "https": "443"}.get(scheme, "")


def same_origin(origin: str, host: str) -> bool:
    """`Origin` が、要求の届いた先(`Host`)と同じ場所を指しているか。

    スキームは比べない —— TLS を終端するリバースプロキシの裏では、Chiezo から見える
    スキーム(http)とブラウザのオリジン(https)が食い違う。名前とポートが同じなら、
    同じ場所の画面から来たものとして扱う(省略されたポートはスキームの既定で補う)。
    """
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    o_name = parts.hostname.lower()
    o_port = str(parts.port) if parts.port else _default_port(parts.scheme)
    h_name, h_port = split_host(host)
    if o_name != h_name:
        return False
    # Host にポートが無いとき、どちらのスキームの既定で来たかは分からない
    return h_port == o_port or (not h_port and o_port in ("80", "443"))


def refuse_reason(method: str, headers, host: str) -> str:
    """状態を変える要求を断る理由。通すなら空文字。

    `Sec-Fetch-Site: cross-site` は別のサイトのページから来た印(ブラウザが付け、
    ページの JS からは書き換えられない)。`Origin` があれば、自分か許したオリジンで
    なければ断る。**どちらも無い要求は通す**(curl・サーバー側のクライアント・MCP)。
    """
    if method.upper() not in UNSAFE_METHODS:
        return ""
    origin = (headers.get("origin") or "").strip().rstrip("/")
    fetch_site = (headers.get("sec-fetch-site") or "").strip().lower()
    listed = bool(origin) and origin.lower() in allowed_origins()
    if fetch_site == "cross-site" and not listed:
        return "別のサイトのページからの書き込みは受け付けません"
    if origin and not listed and not same_origin(origin, host):
        return f"このオリジンからの書き込みは受け付けません: {origin[:80]}"
    return ""


class HostMatcher:
    """MCP SDK の Host 検証に渡す「一覧のふりをした判定」。

    SDK は `host in allowed_hosts` で確かめるだけなので、`__contains__` で
    `host_allowed` を答えれば、本体と同じ既定(IP と localhost とサービス名は通す)を
    そのまま /mcp にも効かせられる。SDK の一覧は完全一致と `名前:*` しか書けず、
    「IP なら何でも」を表せないため。
    """

    def __contains__(self, value: object) -> bool:
        return isinstance(value, str) and host_allowed(value)

    def __iter__(self):
        # SDK はワイルドカードを探して回す。こちらの判定は __contains__ で済んでいる
        return iter(())


class OriginMatcher:
    """MCP SDK の Origin 検証に渡す判定。許したオリジンか、名前が Host として通るもの。

    状態を変える要求の厳しい検査(自分のオリジンか)は本体の中間層が先に掛けるので、
    ここは SDK の検査を外さずに残すための緩い側。
    """

    def __contains__(self, value: object) -> bool:
        if not isinstance(value, str):
            return False
        origin = value.rstrip("/").lower()
        if origin in allowed_origins():
            return True
        parts = urlsplit(origin)
        return parts.scheme in ("http", "https") and host_allowed(parts.netloc)

    def __iter__(self):
        return iter(())
