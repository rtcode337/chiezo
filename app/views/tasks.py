"""やること画面を本体(`chiezo-app`)にも埋め込む(`/tasks`)。

外に出す面(`app/tasks_app.py` = `chiezo-tasks`)と**同じ成果物・同じ REST** を、
認証なしで出すだけの入口。本体は LAN 内・認証なしの前提で、そこには既に
メモを消せる口も取り込みを起こせる管理画面もある —— やること層だけ認証で守っても
守れるものが増えないので、**ここでは素通しにする**。外へ公開する面は今までどおり
`chiezo-tasks` を別プロセスで立てて、そちらだけが認証を持つ。

配り方は外向きの面と 2 つ違う:

- **総取りにしない**。SPA は `/tasks` の下だけに置く。本体には `/v1/**`・`/admin`・
  `/search/**` … と機械向けの口が並んでいて、総取りを足すと綴りを間違えた API 呼び出しに
  殻が 200 で返る(外向きの面が `API_PREFIXES` で避けているのと同じ事故が、
  ここでは避けようのない広さで起きる)
- **PWA にしない**。Service Worker のスコープはルート直下なので、登録させると
  本体の画面まで巻き込む。殻から登録スクリプトと manifest の link を外して配る

`<base href="/tasks/">` を差し込むのは、SPA のルーターがそれを読んで自分の居場所を
決めるため(`tasks-frontend/src/router.ts`)。**資材は絶対パスのまま**なので、
`/assets` と `/icons` は本体側でも配る必要がある(`<base>` は絶対パスに効かない)。
"""
from __future__ import annotations

import re

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from app.tasks_static import cache_headers, resolve, static_dir

router = APIRouter()

# 埋め込み先。SPA のルーターもここを基準に動く(末尾のスラッシュまで含めて渡す)
MOUNT = "/tasks"

# 殻から外すもの。どちらもルート直下を掴みに行くので、本体に埋め込むときは邪魔になる
_DROP_FROM_SHELL = (
    re.compile(r'<script id="vite-plugin-pwa:register-sw"[^>]*></script>'),
    re.compile(r'<link rel="manifest"[^>]*>'),
)

_MISSING = JSONResponse(
    status_code=503,
    content={"error": "やること画面がまだ置かれていません(tasks-frontend のビルド成果物が要る)"},
)


def _shell() -> HTMLResponse | JSONResponse:
    """殻(index.html)を埋め込み用に加工して返す。"""
    index = static_dir() / "index.html"
    if not index.is_file():
        return _MISSING
    html = index.read_text(encoding="utf-8")
    html = html.replace("<head>", f'<head>\n    <base href="{MOUNT}/">', 1)
    for pattern in _DROP_FROM_SHELL:
        html = pattern.sub("", html)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@router.get("/tasks")
def tasks_root() -> RedirectResponse:
    """末尾のスラッシュへ寄せる。`<base>` の解決が `/tasks/` を基準にするため。"""
    return RedirectResponse(url=f"{MOUNT}/", status_code=307)


@router.get("/tasks/{path:path}")
def tasks_spa(path: str):
    """`/tasks` 配下。実ファイルがあればそれ、無ければ殻(画面側のルーティング)。"""
    root = static_dir()
    if not root.is_dir():
        return _MISSING
    file = resolve(root, path)
    if file is not None and file.name != "index.html":
        return FileResponse(file, headers=cache_headers(file, root))
    return _shell()


def _asset(prefix: str, path: str):
    """画面の資材。殻が絶対パスで参照するので `/tasks` の外にも要る。

    `<base>` は絶対パス(`/assets/…`)の解決には効かないため、ビルドし直して
    相対にしない限りここに置くしかない。本体は `/assets` も `/icons` も
    使っていないので衝突しない。
    """
    root = static_dir()
    if not root.is_dir():
        return _MISSING
    file = resolve(root, f"{prefix}/{path}")
    if file is None:
        return JSONResponse(status_code=404, content={"error": "not found"})
    return FileResponse(file, headers=cache_headers(file, root))


@router.get("/assets/{path:path}")
def tasks_assets(path: str):
    return _asset("assets", path)


@router.get("/icons/{path:path}")
def tasks_icons(path: str):
    return _asset("icons", path)
