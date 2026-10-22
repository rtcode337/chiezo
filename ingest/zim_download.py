"""Wikipedia のオフライン版(Kiwix の ZIM)を落とす(chiezo-trigger が受け持つ)。

chiezo-app は置いてある ZIM から記事の絵を引く(`app/wiki_zim.py`)。その ZIM を画面から
落とせるようにするための 1 本で、**取り込みと同じ枠(ダンプの枠)で走る** —— 数十 GB を
何時間もかけて落とすので、chiezo-app の中で抱えず、`/data` に書けて長い作業を受け持つ
こちらに置く。進み具合・止める・続きから落とすは、ダンプのダウンロードと同じ部品
(`download.fetch`)をそのまま使う。

- **どの版を落とすかは Kiwix の一覧から決める**(`EDITION` の一番新しい年月)。
  一覧は `LIST_TTL` のあいだ覚える(状況の面を開くたびに外へ聞きに行かない)
- **空きが足りなければ落とし始めない**(`run`)。途中まで落としたぶんは数に入れる
- **新しい版が揃ったら古い版を消す**(2 つ並べると倍の置き場を食う)。揃うまでは古い版で引ける
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("chiezo.ingest.zim")

LIST_URL = "https://download.kiwix.org/zim/wikipedia/"
# 日本語版の画像入り(nopic・mini には絵が入っていない)
EDITION = "wikipedia_ja_all_maxi"
_NAME = re.compile(rf"{EDITION}_(\d{{4}}-\d{{2}})\.zim")
# 一覧を覚える長さ(秒)。版は数か月おきにしか出ない
LIST_TTL = 6 * 3600
# 落とし終えたあとにも残しておく空き(バイト)。ディスクを使い切らない
SPARE_BYTES = 2 * 1024**3
TIMEOUT = 60

_lock = threading.Lock()
_cache: tuple[float, dict | None] = (0.0, None)
# 裏で一覧を読み直しているあいだ取る(重ねて読みに行かない)
_refreshing = threading.Lock()


def zim_dir() -> Path:
    """置き場。chiezo-app が読む場所(`CHIEZO_ZIM_DIR`、既定 `/data/zim`)と同じにする。"""
    return Path(os.environ.get("CHIEZO_ZIM_DIR") or "/data/zim")


def _read(url: str, method: str = "GET"):
    from download import USER_AGENT

    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def latest(refresh: bool = False) -> dict | None:
    """Kiwix にある一番新しい版。`{"name", "url", "bytes"}`(大きさが分からなければ `bytes` は None)。

    一覧が読めなければ None(`LIST_TTL` のあいだは前に読んだ結果を返す)。
    """
    global _cache
    with _lock:
        at, found = _cache
        if not refresh and found is not None and time.monotonic() - at < LIST_TTL:
            return dict(found)
    try:
        with _read(LIST_URL) as res:
            listing = res.read().decode("utf-8", "replace")
    except OSError as e:
        log.info("could not read the Kiwix list: %s", e)
        return None
    months = sorted({m.group(1) for m in _NAME.finditer(listing)})
    if not months:
        log.info("no %s in the Kiwix list", EDITION)
        return None
    name = f"{EDITION}_{months[-1]}.zim"
    url = LIST_URL + name
    size = None
    try:
        with _read(url, "HEAD") as res:
            size = int(res.headers.get("Content-Length") or 0) or None
    except (OSError, ValueError) as e:
        log.info("could not read the size of %s: %s", name, e)
    found = {"name": name, "url": url, "bytes": size}
    with _lock:
        _cache = (time.monotonic(), found)
    return dict(found)


def peek() -> dict | None:
    """覚えている一番新しい版を**待たずに**返す。古いか無ければ、裏で読み直しを始める
    (状況の面から聞かれるので、外へ聞きに行くのを待たせない。初めの 1 回は None)。"""
    with _lock:
        at, found = _cache
        fresh = found is not None and time.monotonic() - at < LIST_TTL
    if not fresh and _refreshing.acquire(blocking=False):

        def refresh():
            try:
                latest(refresh=True)
            finally:
                _refreshing.release()

        threading.Thread(target=refresh, daemon=True).start()
    return dict(found) if found else None


def placed() -> list[dict]:
    """置いてある版と、途中まで落としたもの(`{"name", "bytes", "partial"}`)。"""
    folder = zim_dir()
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob(f"{EDITION}_*.zim*")):
        if path.name.endswith(".zim"):
            out.append({"name": path.name, "bytes": path.stat().st_size, "partial": False})
        elif path.name.endswith(".zim.part"):
            out.append({"name": path.name[: -len(".part")], "bytes": path.stat().st_size, "partial": True})
    return out


def free_bytes() -> int | None:
    """置き場のディスクの空き。置き場がまだ無ければ、その親で数える。"""
    folder = zim_dir()
    target = folder if folder.is_dir() else folder.parent
    try:
        return shutil.disk_usage(target).free
    except OSError:
        return None


def needed_bytes(info: dict) -> int | None:
    """その版を落とし切るのに、あと要る空き(途中まで落としたぶんを引き、`SPARE_BYTES` を足す)。"""
    if not info.get("bytes"):
        return None
    part = zim_dir() / (info["name"] + ".part")
    have = part.stat().st_size if part.exists() else 0
    return max(info["bytes"] - have, 0) + SPARE_BYTES


def run() -> None:
    """一番新しい版を落とし、揃ったら古い版を消す。もう持っていれば何もしない。"""
    import download

    from core import check_stop

    info = latest(refresh=True)
    if info is None:
        raise SystemExit("Kiwix の一覧を読めませんでした(つながらないか、日本語版の画像入りが載っていません)")
    folder = zim_dir()
    dest = folder / info["name"]
    if dest.exists():
        log.info("already have the latest offline Wikipedia: %s", dest.name)
    else:
        need, free = needed_bytes(info), free_bytes()
        if need is not None and free is not None and free < need:
            raise SystemExit(
                f"置き場の空きが足りません(あと {need / 1024**3:,.1f} GiB 要るのに、空きは {free / 1024**3:,.1f} GiB)"
            )
        check_stop()
        folder.mkdir(parents=True, exist_ok=True)
        download.fetch(info["url"], dest)
    _remove_older(dest)


def _remove_older(keep: Path) -> None:
    """揃った版のほかの版と、途中まで落としていた別の版を消す。"""
    for path in zim_dir().glob(f"{EDITION}_*.zim*"):
        if path.name == keep.name:
            continue
        log.info("removing the older offline Wikipedia: %s", path.name)
        path.unlink(missing_ok=True)
