"""管理画面・ソース閲覧画面で共通の HTML 組み立てヘルパー。"""
from __future__ import annotations

import html

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
<title>{html.escape(title)}</title>
<style>{PAGE_STYLE}</style>
</head>
<body>
{body}
</body>
</html>"""


def esc(value) -> str:
    return html.escape(str(value)) if value is not None else ""
