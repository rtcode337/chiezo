"""管理画面・ソース閲覧画面で共通の HTML 組み立てヘルパー。"""
from __future__ import annotations

import html

# プロジェクトアイコン(assets/icon.svg が原本。api イメージのビルドコンテキストは api/ のみで
# assets/ を含まないため、最小化した data URI をここに埋め込んで配信する。原本を変えたら更新)
FAVICON_DATA_URI = (
    "data:image/svg+xml;base64,"
    "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNTYgMjU2Ij48cmVjdCB3aWR0aD0iMjU2IiBoZWlnaHQ9IjI1NiIgcng9IjU2IiBmaWxsPSIjNTU2MEUwIi8+PGxpbmUgeDE9IjEyOCIgeTE9IjM0IiB4Mj0iMTI4IiB5Mj0iNTIiIHN0cm9rZT0iI0M5RDFGMiIgc3Ryb2tlLXdpZHRoPSI5IiBzdHJva2UtbGluZWNhcD0icm91bmQiLz48Y2lyY2xlIGN4PSIxMjgiIGN5PSIyOCIgcj0iMTAiIGZpbGw9IiNGRkQ2NEQiLz48cmVjdCB4PSI0NiIgeT0iODAiIHdpZHRoPSIxNiIgaGVpZ2h0PSIzOCIgcng9IjgiIGZpbGw9IiNDOUQxRjIiLz48cmVjdCB4PSIxOTQiIHk9IjgwIiB3aWR0aD0iMTYiIGhlaWdodD0iMzgiIHJ4PSI4IiBmaWxsPSIjQzlEMUYyIi8+PHJlY3QgeD0iNjIiIHk9IjUyIiB3aWR0aD0iMTMyIiBoZWlnaHQ9Ijk2IiByeD0iMjYiIGZpbGw9IiNGNEY2RkYiLz48ZWxsaXBzZSBjeD0iMTAyIiBjeT0iOTgiIHJ4PSIxMiIgcnk9IjEzIiBmaWxsPSIjMkEyRTQzIi8+PGVsbGlwc2UgY3g9IjE1NCIgY3k9Ijk4IiByeD0iMTIiIHJ5PSIxMyIgZmlsbD0iIzJBMkU0MyIvPjxjaXJjbGUgY3g9IjEwNSIgY3k9Ijk0IiByPSI0IiBmaWxsPSIjRkZGRkZGIi8+PGNpcmNsZSBjeD0iMTU3IiBjeT0iOTQiIHI9IjQiIGZpbGw9IiNGRkZGRkYiLz48Zz48cGF0aCBkPSJNMTI4IDE3NiBDMTA4IDE2MiwgNzYgMTYwLCA1NCAxNjggTDU0IDIxNCBDNzYgMjA2LCAxMDggMjA4LCAxMjggMjIyIFoiIGZpbGw9IiNGRkZGRkYiLz48cGF0aCBkPSJNMTI4IDE3NiBDMTQ4IDE2MiwgMTgwIDE2MCwgMjAyIDE2OCBMMjAyIDIxNCBDMTgwIDIwNiwgMTQ4IDIwOCwgMTI4IDIyMiBaIiBmaWxsPSIjRUNFRkZDIi8+PGxpbmUgeDE9IjEyOCIgeTE9IjE3NiIgeDI9IjEyOCIgeTI9IjIyMiIgc3Ryb2tlPSIjQzlEMUYyIiBzdHJva2Utd2lkdGg9IjUiLz48ZyBzdHJva2U9IiNBRUI4RTgiIHN0cm9rZS13aWR0aD0iNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIj48bGluZSB4MT0iNzAiIHkxPSIxODAiIHgyPSIxMTIiIHkyPSIxODYiLz48bGluZSB4MT0iNzAiIHkxPSIxOTQiIHgyPSIxMTIiIHkyPSIyMDAiLz48bGluZSB4MT0iMTQ0IiB5MT0iMTg2IiB4Mj0iMTg2IiB5Mj0iMTgwIi8+PGxpbmUgeDE9IjE0NCIgeTE9IjIwMCIgeDI9IjE4NiIgeTI9IjE5NCIvPjwvZz48L2c+PC9zdmc+"
)

PAGE_STYLE = """
  body { font-family: system-ui, sans-serif; margin: 2rem; color: #222; }
  h1 { font-size: 1.25rem; }
  h2 { font-size: 1.05rem; margin-top: 2rem; }
  table { border-collapse: collapse; margin-top: 1rem; width: 100%; }
  th, td { border: 1px solid #ccc; padding: 0.4rem 0.8rem; text-align: left; vertical-align: top; }
  th { background: #f0f0f0; }
  nav a { margin-right: 1rem; }
  form.init-form { display: inline; }
  .job-status { border: 1px solid #ccc; padding: 0.6rem 1rem; margin-top: 1rem; background: #fafafa; }
  .job-status.running { border-color: #d9a400; background: #fff8e1; }
  .job-status.error { border-color: #c0392b; background: #fdecea; }
  .log-tail { max-height: 12rem; overflow-y: auto; background: #111; color: #ddd;
              padding: 0.6rem; font-family: monospace; font-size: 0.85rem; white-space: pre-wrap; }
  .snippet { color: #555; }
  .muted { color: #666; font-size: 0.85rem; }
  details { margin-top: 1rem; }
  details > summary { cursor: pointer; font-weight: bold; padding: 0.3rem 0; }
  pre.doc-body { white-space: pre-wrap; word-break: break-word; }
  input[type=text] { padding: 0.3rem 0.5rem; width: 20rem; }
  button { padding: 0.3rem 0.8rem; }
"""


def page_shell(title: str, body: str, refresh: int | None = None) -> str:
    refresh_tag = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
{refresh_tag}
<link rel="icon" href="{FAVICON_DATA_URI}">
<title>{html.escape(title)}</title>
<style>{PAGE_STYLE}</style>
</head>
<body>
{body}
</body>
</html>"""


def esc(value) -> str:
    return html.escape(str(value)) if value is not None else ""
