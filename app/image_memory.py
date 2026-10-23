"""外の絵を確かめた結果を覚える置き場(短期記憶の名前付きの置き場 `chiezo_images`)。

## 何のためか

外の絵の写し(`app/thumbs.py`。`state/thumbs`)は、取れた絵なら 2 度目から外へ出ない。
ところが**取れなかった絵は覚えていなかった** —— 頼まれるたびに相手へ聞きに行き、読む側の
アプリが 2 つ(本番と手元など)あると、どちらも同じ絵を何度も聞いた(本の表紙で、版元
ドットコムに無い本を手元の画面を開くたびに聞き直していた)。ここに絵の URL ごとの結果
(取れた・無かった)を 1 件ずつ残し、**無かった絵は確かめ直す日まで外へ出ずに断る**。
表紙・写真・収集のサムネイルのどれでも同じ(`thumbs.make` を通るもの全部)。

## 形

- 1 枚 = 1 件。見出しは絵の URL の sha1(写しの名前と同じ。URL は長く、見出しの上限で切れて
  ぶつかるため)、本文に URL と結果、タグは `取れた` / `無かった` と `ホスト:<相手>`、脇書きに
  `url`・`thumb`(写しの場所)・`how`(オフライン版から取った・取った)・`reason`(無かった理由)・
  `checked_at`(UTC)
- **短期記憶の名前付きの置き場**なので、記憶の面の表に件数が並び、
  `/v1/chiezo_images/filter?tag=無かった` やブラウズ画面で引ける。書くのは必ず `notes.add` /
  `notes.update` を通す(索引がずれないように)。起動のときに置き場を作る(`main.lifespan`)
- **無かった絵は `RECHECK_DAYS` 経ったら確かめ直す**(後から絵が置かれることがある)
- **一時的な失敗は覚えない**(つながらない・5xx・429・休み中)—— 取り直せば取れるかもしれない
- 置き場(`CHIEZO_NOTES_DIR`)が無い構成では覚えない(今までどおり毎回取りに行く)
"""
from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from app import notes

log = logging.getLogger("chiezo.app")

STORE = notes.IMAGES_STORE
FOUND_TAG = "取れた"
MISSING_TAG = "無かった"
# 無かった絵を確かめ直すまでの日数
RECHECK_DAYS = 30


def is_enabled() -> bool:
    return notes.notes_dir() is not None


def key_of(url: str) -> str:
    """覚えるときの見出し(写しの名前と同じ sha1)。"""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def _row(key: str):
    path = notes.notes_path(STORE)
    if path is None or not path.is_file():
        return None
    conn = notes._connect(path)
    try:
        return conn.execute("SELECT doc_id, tags, extra FROM docs WHERE title = ?", (key,)).fetchone()
    finally:
        conn.close()


def recall(url: str) -> dict | None:
    """覚えている結果。`{"found", "thumb", "how", "reason", "checked_at"}`。覚えていなければ None。"""
    if not is_enabled():
        return None
    try:
        row = _row(key_of(url))
    except Exception:
        log.warning("image_memory: 覚えている結果を読めなかった", exc_info=True)
        return None
    if row is None:
        return None
    extra = notes.load_extra(row["extra"]) or {}
    return {
        "found": FOUND_TAG in (row["tags"] or ""),
        "thumb": str(extra.get("thumb") or ""),
        "how": str(extra.get("how") or ""),
        "reason": str(extra.get("reason") or ""),
        "checked_at": str(extra.get("checked_at") or ""),
    }


def still_missing(url: str, now: datetime | None = None) -> dict | None:
    """無かったと覚えていて、確かめ直す日がまだ来ていなければその結果。それ以外は None。"""
    entry = recall(url)
    if not entry or entry["found"]:
        return None
    try:
        checked = datetime.fromisoformat(entry["checked_at"])
    except ValueError:
        return None
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=UTC)
    fresh = (now or datetime.now(UTC)) - checked < timedelta(days=RECHECK_DAYS)
    return entry if fresh else None


def remember(url: str, *, thumb: str = "", how: str = "", reason: str = "") -> None:
    """結果を残す(`thumb` が空なら「無かった」)。**残せなくても絵を作るのは止めない**。"""
    if not is_enabled():
        return
    found = bool(thumb)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    extra = {"url": url, "checked_at": now}
    extra.update({"thumb": thumb, "how": how} if found else {"reason": reason})
    text = (
        f"{url}\n取れた({how})。写し: {thumb}"
        if found
        else f"{url}\n取れなかった({reason})。{RECHECK_DAYS} 日経ったら確かめ直す"
    )
    host = urlparse(url).hostname or ""
    tags = ",".join(t for t in (FOUND_TAG if found else MISSING_TAG, f"ホスト:{host}" if host else "") if t)
    try:
        notes.ensure_db(STORE)
        row = _row(key_of(url))
        if row is None:
            notes.add(text, title=key_of(url), tags=tags, extra=extra, store=STORE)
        else:
            notes.update(row["doc_id"], text=text, tags=tags, extra=extra, store=STORE)
    except Exception:
        log.warning("image_memory: 結果を残せなかった: %s", url, exc_info=True)
