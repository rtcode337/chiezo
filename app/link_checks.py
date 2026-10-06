"""1 件の URL を確かめる —— 開けるか・どこへ転送されるか・頁の題名。

## 何のためか

収集の 1 件が持つ URL(`extra.url` / `extra.website`)は、AI が書いたものと辞典から
来たものが混ざる。**どちらも開けるとは限らない** —— AI は口コミサイトの店の番号を
推し量って書くことがあり(1 つ違うと別の店の頁になる)、地図辞典の公式サイト欄には
何年も前に書かれたまま、ドメインごと無くなったものが残る。読む側がそのまま人に
見せると、押しても開かないリンクや別の店の頁が並ぶ。

ここでは URL を 1 回開いて、**事実だけを控える**(状態・転送先・頁の題名)。
その URL を使うかどうかは読む側が決める —— 題名が店の名前と合っているべきかは
サイトによる(チェーンの公式サイトは会社の名前を題名にする)ので、決まりを
ここに持たせない。

## 文書には書かない

控えるのは Chiezo の手元(`state/links.db`)で、1 件の脇書きには載せない。
脇書きに載せるには焼き直しが要り、名簿の回は週に 1 度しか回らない収集もある ——
それでは確かめた結果がいつまでも届かない。控えは URL で引くので、同じ URL を
複数の 1 件が持っていても 1 回で済む。

## 進め方

**有効にした収集だけ**(`Collection.links`)。時計(`tick`)が回るたびに、まだ
確かめていない URL・確かめてから日が経った URL を `MAX_PER_TICK` 件ずつ開く。

- **開けた** → `ok`。転送されたら転送先も控える(http から https へ移ったものは、
  読む側が https のほうを使える)
- **開けない**(404・名前を引けないなど)→ 理由を控える。**ただし断られた
  (401・403・429)は「開けない」ではない** —— 機械の取得を断るサイトでも、
  人のブラウザでは開ける
- **一時的なもの**(つながらない・5xx)は数えるだけで、`MAX_TRANSIENT` 回続いたら
  開けないとみなす(相手が落ちているだけの回で消さない)

外へ出る作法は絵の取得と同じ(`app/thumbs.py` の部品を使う)—— 名乗りは
プロジェクト名だけ、1 秒に 1 回まで、手元のネットワークは叩かない、
転送も 1 段ずつ確かめる、頁は頭だけ読む。
"""
from __future__ import annotations

import html
import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from app import db, settings_store, thumbs

log = logging.getLogger("chiezo.app")

# 1 回の時計で開く数(1 秒に 1 回なので、1 回の時計がこの秒数ほどかかる)
MAX_PER_TICK = 20
# 確かめ直すまでの日数(リンクは腐るので、一度確かめたものも時々見直す)
RECHECK_DAYS = 30
# 一時的な失敗がこの回数続いたら、開けないとみなす
MAX_TRANSIENT = 3
# 断られた(機械の取得を拒む)ときの状態。開けないとは区別する
REFUSED = (401, 403, 429)
# 確かめる脇書きの鍵
KEYS = ("url", "website")

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_OG_TITLE = re.compile(
    r"""<meta\s[^>]*?(?:property|name)\s*=\s*["']og:title["'][^>]*>""", re.I
)
_CONTENT = re.compile(r"""content\s*=\s*["']([^"']*)["']""", re.I)
_CHARSET = re.compile(rb"""charset\s*=\s*["']?([\w-]+)""", re.I)


def db_path() -> Path | None:
    state = settings_store.state_dir()
    return state / "links.db" if state else None


def _connect() -> sqlite3.Connection | None:
    path = db_path()
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE IF NOT EXISTS links ("
        " url TEXT PRIMARY KEY, status TEXT NOT NULL, final_url TEXT, title TEXT,"
        " transient INTEGER NOT NULL DEFAULT 0, checked_at TEXT NOT NULL)"
    )
    return con


def _now() -> datetime:
    return datetime.now(UTC)


def lookup(urls) -> dict[str, dict]:
    """控えてある結果(URL → `{status, final_url, title, checked_at}`)。

    **確かめていない URL は入らない**(読む側は「まだ分からない」として扱う)。
    一時的な失敗が続いている途中のものも入らない。
    """
    wanted = sorted({u for u in urls if u})
    con = _connect()
    if con is None or not wanted:
        return {}
    out: dict[str, dict] = {}
    try:
        for at in range(0, len(wanted), 500):
            chunk = wanted[at:at + 500]
            rows = con.execute(
                f"SELECT * FROM links WHERE url IN ({','.join('?' * len(chunk))})"
                " AND status != 'transient'",
                chunk,
            ).fetchall()
            for row in rows:
                out[row["url"]] = {
                    "status": row["status"],
                    "final_url": row["final_url"] or "",
                    "title": row["title"] or "",
                    "checked_at": row["checked_at"],
                }
    finally:
        con.close()
    return out


def _due(con: sqlite3.Connection, urls: list[str], limit: int) -> list[str]:
    """まだ確かめていない URL と、確かめてから日が経った URL(古いものから)。"""
    stale = (_now() - timedelta(days=RECHECK_DAYS)).isoformat()
    known = {
        row["url"]: row
        for at in range(0, len(urls), 500)
        for row in con.execute(
            f"SELECT url, status, checked_at FROM links WHERE url IN "
            f"({','.join('?' * len(urls[at:at + 500]))})",
            urls[at:at + 500],
        ).fetchall()
    }
    fresh = [u for u in urls if u not in known]
    # 一時的な失敗の途中のものは、日を置かずに次の時計で開き直す
    retry = [u for u in urls if u in known and known[u]["status"] == "transient"]
    old = sorted(
        (u for u in urls if u in known and known[u]["status"] != "transient"
         and known[u]["checked_at"] < stale),
        key=lambda u: known[u]["checked_at"],
    )
    return (fresh + retry + old)[:limit]


def collection_urls(path) -> list[str]:
    """収集の長期記憶にある URL(消えたものを除く)。"""
    rows = db.query(
        path,
        "SELECT json_extract(extra, '$.url') AS url, json_extract(extra, '$.website') AS website,"
        " tags FROM docs",
        (),
    )
    out: list[str] = []
    for row in rows:
        if '"_chiezo_removed"' in (row["tags"] or ""):
            continue
        for key in KEYS:
            value = str(row[key] or "").strip()
            if value.startswith(("http://", "https://")):
                out.append(value)
    return list(dict.fromkeys(out))


def page_title(body: bytes, content_type: str) -> str:
    """頁の題名(`og:title` があればそちら)。文字コードは見出しか頁の頭から読む。"""
    match = _CHARSET.search(content_type.encode()) or _CHARSET.search(body[:4096])
    encoding = match.group(1).decode() if match else "utf-8"
    try:
        text = body.decode(encoding, "replace")
    except LookupError:
        text = body.decode("utf-8", "replace")
    for tag in _OG_TITLE.findall(text):
        if found := _CONTENT.search(tag):
            return html.unescape(found.group(1)).strip()[:200]
    if found := _TITLE.search(text):
        return html.unescape(re.sub(r"\s+", " ", found.group(1))).strip()[:200]
    return ""


async def check(client: httpx.AsyncClient, url: str) -> tuple[str, str, str]:
    """1 つ開く。`(状態, 転送先, 題名)`。状態は `ok` / 理由 / `transient`。"""
    try:
        final, body, content_type = await thumbs._get(client, url, thumbs.MAX_PAGE_BYTES)
    except thumbs.Skip as e:
        reason = str(e)
        code = reason.removeprefix("HTTP ").strip()
        if code.isdigit() and int(code) in REFUSED:
            return f"断られた({reason})", "", ""
        return reason, "", ""
    except (httpx.HTTPError, OSError) as e:
        log.info("links: 開けなかった(次の時計で開き直す): %s: %s", url, e)
        return "transient", "", ""
    return "ok", final if final != url else "", page_title(body, content_type)


def _save(con: sqlite3.Connection, url: str, status: str, final: str, title: str) -> None:
    now = _now().isoformat()
    if status == "transient":
        row = con.execute("SELECT transient FROM links WHERE url = ?", (url,)).fetchone()
        count = (row["transient"] if row else 0) + 1
        if count >= MAX_TRANSIENT:
            status = f"つながらない({count} 回続いた)"
        con.execute(
            "INSERT INTO links (url, status, final_url, title, transient, checked_at)"
            " VALUES (?, ?, '', '', ?, ?) ON CONFLICT(url) DO UPDATE SET"
            " status = excluded.status, transient = excluded.transient,"
            " checked_at = excluded.checked_at",
            (url, status, count, now),
        )
    else:
        con.execute(
            "INSERT INTO links (url, status, final_url, title, transient, checked_at)"
            " VALUES (?, ?, ?, ?, 0, ?) ON CONFLICT(url) DO UPDATE SET"
            " status = excluded.status, final_url = excluded.final_url,"
            " title = excluded.title, transient = 0, checked_at = excluded.checked_at",
            (url, status, final, title, now),
        )
    con.commit()


async def tick(paths: list) -> int:
    """有効にした収集の URL を、`MAX_PER_TICK` 件まで確かめる。確かめた数を返す。

    `paths` は有効にした収集の長期記憶のパス(呼ぶ側が選ぶ)。
    """
    con = _connect()
    if con is None or not paths:
        return 0
    try:
        urls: list[str] = []
        for path in paths:
            urls.extend(collection_urls(path))
        due = _due(con, list(dict.fromkeys(urls)), MAX_PER_TICK)
        if not due:
            return 0
        async with thumbs._client() as client:
            for url in due:
                status, final, title = await check(client, url)
                _save(con, url, status, final, title)
        return len(due)
    finally:
        con.close()
