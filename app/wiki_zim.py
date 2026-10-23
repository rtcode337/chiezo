"""Wikipedia のオフライン版(Kiwix の ZIM)から、記事の絵を引く。

## 何のためか

収集の 1 件や読む側のアプリが指す Wikimedia の絵(`upload.wikimedia.org`)は、1 枚ずつ
取りに行くと**作り置きの無い縮小版を作らせる頼み**になり、Wikimedia は回線ごとに数えて
429 で絞る。断られて休むたびに埋まるのが止まり、写真が何日も埋まらなかった。
Kiwix が配る Wikipedia のオフライン版(`wikipedia_<言語>_all_maxi_<年月>.zim`)には、
記事に載っている絵が縮めた形(WebP。長辺は記事の表示の 2 倍ほど)で入っているので、
**ここにある絵は外へ出ずに引く**(`app/thumbs.py` が外へ取りに行く前に聞く)。
無い絵(オフライン版を作ったあとの記事・記事に載っていない絵)だけ、これまでどおり外へ取りに行く。

## 置き方

`CHIEZO_ZIM_DIR`(既定 `/data/zim`)に `.zim` を置く(複数あれば全部から引く。新しい
ファイル名が先)。**解凍しない** —— ZIM は 1 件ずつ取り出して読める形式なので、置き場は
ファイルそのものだけで済む。置き換えたら次に引くときに開き直す(ファイルの一覧と更新時刻で見る)。
置いていなければ何もしない(外へ取りに行くだけになる)。

## 絵の名前

ZIM の中の絵は `_assets_/<md5("https://upload.wikimedia.org/wikipedia/<プロジェクト>/")>/<ファイル名>`
に入っている(作り手の mwoffliner の決まり。Commons と各言語版で置き場が分かれる)。
ファイル名は URL の名前を `decodeURI` した形 —— 日本語などは戻し、`,` `&` `?` のような予約された
文字は `%2C` のまま残す。URL の側の書き方は出どころで揺れるので、いったん全部戻してから
予約された文字だけを符号に戻した形を先に引き、当たらなければ揺れの形も引く(`asset_names`)。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from pathlib import Path
from urllib.parse import unquote

log = logging.getLogger("chiezo.app")

DEFAULT_DIR = "/data/zim"
# 最新版を落とす 1 本の名前(chiezo-trigger の `ZIM_JOB` と同じ)。定期の取り直しの予定もこの名前で持つ
JOB = "wikipedia_zim"
# 1 枚の上限(バイト)。ZIM の中の絵は縮めてあるのでふつうは数十 KB
MAX_BYTES = 8 * 1024 * 1024
# `decodeURI` が戻さない文字(予約された文字)。ZIM の中の名前ではこれらが符号のまま残る
_RESERVED = ";/?:@&=+$,#"
_WIKIMEDIA = re.compile(
    r"^https://upload\.wikimedia\.org/wikipedia/([a-z-]+)/(?:thumb/)?[0-9a-f]/[0-9a-f]{2}/([^/?#]+)"
)

_lock = threading.Lock()
_opened: tuple[tuple, list] = ((), [])


def zim_dir() -> Path:
    return Path(os.environ.get("CHIEZO_ZIM_DIR") or DEFAULT_DIR)


def _files() -> list[Path]:
    try:
        found = [p for p in zim_dir().glob("*.zim") if p.is_file()]
    except OSError:
        return []
    # 新しいオフライン版を先に引く(名前の末尾が年月)
    return sorted(found, key=lambda p: p.name, reverse=True)


def _archives() -> list:
    """開いた ZIM。ファイルの一覧か更新時刻が変わったら開き直す。"""
    global _opened
    files = _files()
    key = tuple((str(p), p.stat().st_mtime_ns) for p in files)
    with _lock:
        if key == _opened[0]:
            return _opened[1]
        try:
            from libzim.reader import Archive  # 重いので使うときに読む
        except ImportError:
            log.warning("wiki_zim: libzim が無いので ZIM を読めません")
            _opened = (key, [])
            return []
        opened = []
        for path in files:
            try:
                opened.append(Archive(path))
            except RuntimeError:
                log.warning("wiki_zim: ZIM を開けませんでした: %s", path.name, exc_info=True)
        _opened = (key, opened)
        return opened


def _decode_uri(name: str) -> str:
    """全部戻してから、予約された文字だけ符号に戻す(JavaScript の `decodeURI` の結果に揃える)。"""
    return "".join(f"%{ord(c):02X}" if c in _RESERVED else c for c in unquote(name))


def asset_names(url: str) -> list[str]:
    """Wikimedia の絵の URL から、ZIM の中で引く名前の候補(先頭が本命)。当てはまらなければ空。"""
    found = _WIKIMEDIA.match(url or "")
    if not found:
        return []
    project, name = found.groups()
    base = hashlib.md5(f"https://upload.wikimedia.org/wikipedia/{project}/".encode()).hexdigest()
    names = [_decode_uri(name), unquote(name), name]
    return [f"_assets_/{base}/{n}" for n in dict.fromkeys(names)]


def placed() -> bool:
    """オフライン版を 1 つでも置いているか。"""
    return bool(_archives())


def covers(url: str) -> bool:
    """その URL がオフライン版で引ける種類の絵か(Wikimedia の置き場の絵)。"""
    return bool(asset_names(url))


def image(url: str) -> bytes | None:
    """置いた ZIM にその絵があれば中身を返す。無ければ None(外へ取りに行く)。"""
    names = asset_names(url)
    if not names:
        return None
    for archive in _archives():
        for name in names:
            try:
                if not archive.has_entry_by_path(name):
                    continue
                item = archive.get_entry_by_path(name).get_item()
                if not item.mimetype.startswith("image/") or item.size > MAX_BYTES:
                    continue
                return bytes(item.content)
            except RuntimeError:
                log.warning("wiki_zim: ZIM から読めませんでした: %s", name, exc_info=True)
    return None


def status() -> list[dict]:
    """置いてある ZIM(新しい順)。`name`・`bytes`・`date`(オフライン版を作った日)・`media`(絵の数)。"""
    out = []
    for archive in _archives():
        path = Path(str(archive.filename))
        try:
            date = archive.get_metadata("Date").decode()
        except (RuntimeError, KeyError):
            date = ""
        out.append({"name": path.name, "bytes": archive.filesize, "date": date, "media": archive.media_count})
    return out
