"""やること画面(`tasks-frontend` のビルド成果物)の置き場と配信の下ごしらえ。

配る面が 2 つあるので、置き場の解決とキャッシュの決めごとをここに集めてある:

- `app/tasks_app.py` … 外に出す面(`chiezo-tasks`)。ルート直下に SPA を置き、認証で守る
- `app/views/tasks.py` … 本体に埋め込む面(`chiezo-app` の `/tasks`)。LAN 内なので認証なし

**ここは app の他モジュールを import しない**(両方から使われる下ごしらえなので、
`app/deps.py` と同じ立ち位置)。
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

# `tasks-frontend` を `npm run build` した中身。イメージでは Dockerfile が
# ここへ置く(WORKDIR の直下)。手元で試すときは
# `CHIEZO_TASKS_STATIC_DIR=tasks-frontend/dist`。
DEFAULT_STATIC_DIR = "/srv/chiezo/tasks-static"


def static_dir() -> Path:
    return Path(os.environ.get("CHIEZO_TASKS_STATIC_DIR", DEFAULT_STATIC_DIR))


def resolve(root: Path, path: str) -> Path | None:
    """配信してよい実ファイル。**置き場の外を指していたら配らない**。

    URL から来た文字列をそのままパスに使うので、2 段で守る:

    1. **組み立てる前に弾く** —— `..` を含むもの、絶対パス、Windows のドライブ指定
       (`C:`)。ここで落とせば、そもそも外を指すパスを作らない
    2. **解決してから親子関係を確かめる** —— 1 だけではシンボリックリンクを辿った先が
       置き場の外、という抜け道が残る(`resolve()` はリンクを解決するので、
       解決後のパスで見れば塞げる)

    どちらか片方では足りない。1 は意図を明示して静的解析にも追えるようにする段で、
    実際に外を防いでいるのは 2 のほう。
    """
    if not path:
        return None
    candidate_rel = PurePosixPath(path)
    if candidate_rel.is_absolute() or any(part == ".." for part in candidate_rel.parts):
        return None
    if any(":" in part for part in candidate_rel.parts):
        return None
    try:
        base = root.resolve()
        candidate = (base / candidate_rel).resolve()
    except OSError:
        return None
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def cache_headers(file: Path, root: Path) -> dict:
    """Vite が名前にハッシュを付けた資材だけ長く持たせる。

    殻(index.html)と Service Worker を長く持たせると、更新しても古い版が
    出続ける。逆にハッシュ付きの資材は中身が変われば名前も変わるので、
    長く持たせても取り違えない。
    """
    if file.parent == root.resolve() / "assets":
        return {"Cache-Control": "public, max-age=31536000, immutable"}
    return {"Cache-Control": "no-cache"}
