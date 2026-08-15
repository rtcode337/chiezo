"""管理画面・ソース閲覧画面で共通の HTML 組み立てヘルパー。"""
from __future__ import annotations

import base64
import html
from urllib.parse import quote

# ブラウズ画面の URL。**ソースの画面は `/search/` の下に置く**(ルート直下を
# キャッチオールにすると、`ask` や `admin` という名前のソースを足せなくなる)。
# 出典のリンクもここを通すので、移すときに漏れない。
BROWSE_PREFIX = "/search"


def browse_url(source: str, suffix: str = "") -> str:
    return f"{BROWSE_PREFIX}/{quote(str(source))}/{suffix}"


def doc_url(source: str, doc_id: int) -> str:
    return browse_url(source, f"doc/{doc_id}")

# プロジェクトアイコン(assets/icon.svg が原本。api イメージのビルドコンテキストは api/ のみで
# assets/ を含まないため、最小化した data URI をここに埋め込んで配信する。原本を変えたら更新)
FAVICON_DATA_URI = (
    "data:image/svg+xml;base64,"
    "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNTYgMjU2Ij48cmVjdCB3aWR0aD0iMjU2IiBoZWlnaHQ9IjI1NiIgcng9IjU2IiBmaWxsPSIjNTU2MEUwIi8+PGxpbmUgeDE9IjEyOCIgeTE9IjM0IiB4Mj0iMTI4IiB5Mj0iNTIiIHN0cm9rZT0iI0M5RDFGMiIgc3Ryb2tlLXdpZHRoPSI5IiBzdHJva2UtbGluZWNhcD0icm91bmQiLz48Y2lyY2xlIGN4PSIxMjgiIGN5PSIyOCIgcj0iMTAiIGZpbGw9IiNGRkQ2NEQiLz48cmVjdCB4PSI0NiIgeT0iODAiIHdpZHRoPSIxNiIgaGVpZ2h0PSIzOCIgcng9IjgiIGZpbGw9IiNDOUQxRjIiLz48cmVjdCB4PSIxOTQiIHk9IjgwIiB3aWR0aD0iMTYiIGhlaWdodD0iMzgiIHJ4PSI4IiBmaWxsPSIjQzlEMUYyIi8+PHJlY3QgeD0iNjIiIHk9IjUyIiB3aWR0aD0iMTMyIiBoZWlnaHQ9Ijk2IiByeD0iMjYiIGZpbGw9IiNGNEY2RkYiLz48ZWxsaXBzZSBjeD0iMTAyIiBjeT0iOTgiIHJ4PSIxMiIgcnk9IjEzIiBmaWxsPSIjMkEyRTQzIi8+PGVsbGlwc2UgY3g9IjE1NCIgY3k9Ijk4IiByeD0iMTIiIHJ5PSIxMyIgZmlsbD0iIzJBMkU0MyIvPjxjaXJjbGUgY3g9IjEwNSIgY3k9Ijk0IiByPSI0IiBmaWxsPSIjRkZGRkZGIi8+PGNpcmNsZSBjeD0iMTU3IiBjeT0iOTQiIHI9IjQiIGZpbGw9IiNGRkZGRkYiLz48Zz48cGF0aCBkPSJNMTI4IDE3NiBDMTA4IDE2MiwgNzYgMTYwLCA1NCAxNjggTDU0IDIxNCBDNzYgMjA2LCAxMDggMjA4LCAxMjggMjIyIFoiIGZpbGw9IiNGRkZGRkYiLz48cGF0aCBkPSJNMTI4IDE3NiBDMTQ4IDE2MiwgMTgwIDE2MCwgMjAyIDE2OCBMMjAyIDIxNCBDMTgwIDIwNiwgMTQ4IDIwOCwgMTI4IDIyMiBaIiBmaWxsPSIjRUNFRkZDIi8+PGxpbmUgeDE9IjEyOCIgeTE9IjE3NiIgeDI9IjEyOCIgeTI9IjIyMiIgc3Ryb2tlPSIjQzlEMUYyIiBzdHJva2Utd2lkdGg9IjUiLz48ZyBzdHJva2U9IiNBRUI4RTgiIHN0cm9rZS13aWR0aD0iNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIj48bGluZSB4MT0iNzAiIHkxPSIxODAiIHgyPSIxMTIiIHkyPSIxODYiLz48bGluZSB4MT0iNzAiIHkxPSIxOTQiIHgyPSIxMTIiIHkyPSIyMDAiLz48bGluZSB4MT0iMTQ0IiB5MT0iMTg2IiB4Mj0iMTg2IiB5Mj0iMTgwIi8+PGxpbmUgeDE9IjE0NCIgeTE9IjIwMCIgeDI9IjE4NiIgeTI9IjE5NCIvPjwvZz48L2c+PC9zdmc+"
)

# iPhone の「ホーム画面に追加」用アイコン(apple-touch-icon)。iOS は SVG や data URI の
# ファビコンをホームアイコンに使わず、PNG の apple-touch-icon だけを見る。原本は同じく
# assets/icon.svg で、iOS が角丸マスクを自前で掛けるため角丸なし・全面塗りで 180x180 に
# ラスタライズしてある(原本を変えたら README「開発」節の手順で再生成)
APPLE_TOUCH_ICON_PNG = base64.b64decode(
    "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"
    "0tJiAMCa1Suq58995rnf+3z+t9/94Pf/8W8J5wUD6o+QJoPT5a6v33P99c2bNlz5maHpjRtuBgBwHL99+xfJZPfpp5+HueFt"
    "wNrF86NmRLlp1TKr1QIA2LVrt8vtSSY71cluOQ42HBalGMOFNDWkRGboy29F7z8wwtFM8dm/v3HEvK5kJ4piY8ORZLJTneyW"
    "o621Peb13XsOXvlZluU9ey//8+zZDp5PsJ7mOL7jvOP660ePNft8V+u1U63tPb2X+9Ktp7P7IJ/sbnN4PN6Y1199/S2WZW+s"
    "WxoIBj/4cNvRH5uj12VZ9ni8RUUj7IcfE693IGYPxeXyPPHUS8/+80MTSyacbGl7/Y13r36U5dVKdstBkLE7fOEw99of33nt"
    "j+9c/5FGk+CfrJQXAOBky+nHf/tijLxG7MfDTXZXK3a7bUzpCYKwWBJ8B8KaZyHGOIw21vBgI7vlqKqqGFP6yspyItFBN4Ig"
    "yufOGdN/mT8v9cP2mSS75VhSW6PVjmHl+o0rb0gmu1Vj+e9anXbx4uw+5Sm75TAYDJvuvn2UiS1m852335pMdnffdVveqGul"
    "ezbfqddD8YZBwmS3HACAxx55YPKU0tGkfOH5p3XJfVtanfZ3v3tmNClLS0se/KffJJMXDGS9HFqd9n//5z9tNmv8ZI8+cv+6"
    "dTcnn92a1Tc++siW+GnsNuurr/wh24sNMA7kAABMnzblvXffrFRonBr0+pdffuHJJx5KVXZPPvHwSy8+r/TdV1VVvPfem9Om"
    "TklVdiqS9o64KI6wYCnW8PeYKSkueu/tN/buPfD11981t5xyuz0MQ0+ZPKmubtmv7tpotealII9ruOvODXUrln6+48t9+w46"
    "LnSFw5zNZq2aW75+/eqVK2/AsBSs0RrxtkhS2leCpX03wcWLmLrl8QpYV5+8crkptZmKophwlxWS7Pbs8xUWx/v6Gw6xDY1s"
    "nATJk/Zq5XQbL4iKq2xkGThdqd9FL5NmpCm7fqfA8/Hu29lzaV8hm3Y5fD6psTGs9GnTCS7EwrgFp+rwEfDDfsWCobWN7+9P"
    "+9aUmWiQHvmRPfZTDD/OtPN7fwhlIIAspak53HCIvX6y73xn5Nvdmdj3OBMzQ7IMvt8X6jgfqVnAFBUS0aqk+STXfpZXWIiJ"
    "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"
    "SUVORK5CYII="
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
  /* column-reverse でスクロール原点を下端にし、リロード直後から最新行が見えるようにする
     (中身は単一のテキストノードなので表示順は変わらない。JS なしで済む) */
  .log-tail { max-height: 12rem; overflow-y: auto; display: flex; flex-direction: column-reverse;
              background: #111; color: #ddd;
              padding: 0.6rem; font-family: monospace; font-size: 0.85rem; white-space: pre-wrap; }
  .snippet { color: #555; }
  .muted { color: #666; font-size: 0.85rem; }
  .pager { display: flex; gap: 1rem; align-items: baseline; margin-top: 1rem; }
  .stale { color: #c0392b; font-size: 0.85rem; }
  /* 管理画面の「話す相手」の表。設定を一度入れたら開かない場所なので、1 行を低く保つ
     (本文と同じ字の大きさだと、手順の説明が入る列で行が伸びすぎる)。 */
  table.ai-settings td { font-size: 0.85rem; }
  table.ai-settings button { padding: 0.2rem 0.6rem; font-size: 0.85rem; }
  table.ai-settings input[type=text] { width: 16rem; font-size: 0.85rem; }
  /* details は本文中の見出し向けの体裁(太字・上下の余白)なので、表の中では抑える。 */
  table.ai-settings details { margin-top: 0.3rem; }
  table.ai-settings details > summary { font-weight: normal; padding: 0.1rem 0; }
  table.ai-settings p { margin: 0.3rem 0; }
  /* **無効な相手は行ごと薄くする。** 「使う」のボタンの文字だけでは、行が増えるほど
     いまどちらの状態か読み取りにくい。ボタンは押せる必要があるので薄くしない。 */
  table.ai-settings tr.off { background: #f7f7f7; }
  table.ai-settings tr.off td { color: #999; }
  table.ai-settings tr.off button { color: inherit; }
  details { margin-top: 1rem; }
  details > summary { cursor: pointer; font-weight: bold; padding: 0.3rem 0; }
  pre.doc-body { white-space: pre-wrap; word-break: break-word; }
  pre.answer { white-space: pre-wrap; word-break: break-word; line-height: 1.7;
               border: 1px solid #ccc; border-left: 4px solid #5560E0;
               background: #fafaff; padding: 0.8rem 1rem; }
  select { padding: 0.3rem 0.5rem; }
  input[type=text] { padding: 0.3rem 0.5rem; width: 20rem; max-width: 100%; box-sizing: border-box; }
  button { padding: 0.3rem 0.8rem; }
  /* 狭い画面。列の多い表(管理画面の登録ソース一覧)がそのままだと画面からはみ出し、
     ページ全体が横スクロールして本文まで画面外へ出る。**表だけ**を横スクロールさせ、
     ページ本体は縦スクロールだけで読めるようにする。
     table を display:block にすると幅が中身なりになり、あふれた分がこの表の中で
     スクロールする(表を <div> で包まずに済むので、画面ごとの HTML 組み立ては触らない)。
     しきい値がスマホ幅より広いのは、**登録ソースの表が約 800px 必要**だから
     (built_at の ISO 日時と schema_version の見出しが長い)—— タブレット縦持ちや
     小さいウィンドウでもあふれる。 */
  @media (max-width: 56rem) {
    table { display: block; overflow-x: auto; }
    th, td { white-space: nowrap; padding: 0.4rem 0.6rem; }
    /* 本文の抜粋とタグは折り返す(1 行に伸ばすと表が果てしなく広くなる) */
    td.snippet { white-space: normal; min-width: 14rem; }
    td.tags { white-space: normal; min-width: 10rem; }
  }
  /* スマホ幅。余白を詰めて本文の幅を稼ぎ、押す物を指の大きさに合わせる */
  @media (max-width: 40rem) {
    body { margin: 1rem 0.9rem; }
    button { padding: 0.5rem 0.9rem; }
    input[type=text] { width: 100%; }
  }
  /* 会話画面の 1 問 1 答(JS なし)版だけが使う。会話そのものの見た目は CHAT_STYLE 側 */
  form.chat { display: flex; gap: 0.5rem; margin-top: 1rem; }
  form.chat input[type=text] { flex: 1; width: auto; }
"""

# 会話画面の URL。画面(app/views/chat.py)・そこへ誘導する管理画面・
# REST 側(app/main.py)が同じ値を見る。
#
# 以前は `/localllm/chat` だった。話す相手はローカルの推論サーバとは限らず
# (Gemini・OpenRouter・CLI ブリッジも選べる)、名前が実態と食い違ったので `/ai/` へ移した。
# **旧 URL のリダイレクトは置いていない** —— LAN 内の画面で、外から参照されるものではないため。
CHAT_PATH = "/ai/chat"

# 会話画面(/ai/chat)専用のスタイル。**管理画面とは分けてある** —
# 管理画面は素っ気ないままでよく、会話は毎日触る画面なので作りが違う。
# PAGE_STYLE の後ろに足すので、body の余白など重なる指定はこちらが勝つ。
CHAT_STYLE = """
  body { margin: 0; background: #faf9f7; color: #23221f; }
  .chat-page { display: flex; flex-direction: column; height: 100dvh;
               max-width: 46rem; margin: 0 auto; padding: 0 1rem; }
  .chat-head { display: flex; align-items: baseline; gap: 1rem; padding: 1rem 0 0.5rem; }
  .chat-head h1 { font-size: 1.05rem; margin: 0; }
  .chat-head .spacer { flex: 1; }
  .chat-head a { color: #6b665e; font-size: 0.85rem; }

  /* 会話は上に伸びて、入力欄は下に貼り付く */
  .log { flex: 1; overflow-y: auto; padding: 0.5rem 0 1rem; scrollbar-width: thin; }
  /* 空のときだけ中央に置く(:has が無い環境では上寄せのまま = 崩れはしない) */
  .log:has(.empty) { display: flex; align-items: center; justify-content: center; }
  .empty { color: #8a857c; font-size: 0.9rem; text-align: center; }
  .empty .examples { display: flex; flex-wrap: wrap; gap: 0.5rem;
                     justify-content: center; margin-top: 1rem; }
  .empty button { background: #fff; border: 1px solid #e3e0da; border-radius: 999px;
                  padding: 0.4rem 0.9rem; font-size: 0.85rem; color: #4a463f;
                  cursor: pointer; }
  .empty button:hover { border-color: #b9b4aa; }

  .turn { margin: 1.1rem 0; display: flex; flex-direction: column; }
  .turn.you { align-items: flex-end; }
  .turn.you .text { background: #ecebff; border-radius: 16px 16px 4px 16px;
                    padding: 0.6rem 0.9rem; max-width: 85%; }
  .turn.bot .text { line-height: 1.75; }
  .turn .text { white-space: pre-wrap; word-break: break-word; }
  /* Markdown を組んだ返事。**pre-wrap を戻す** —— ブロック要素で行が分かれるので、
     改行も残すと段落のあいだが二重に空く */
  .turn .text.md { white-space: normal; }
  .turn .text.md > :first-child { margin-top: 0; }
  .turn .text.md > :last-child { margin-bottom: 0; }
  .turn .text.md p { margin: 0.6rem 0; }
  .turn .text.md h2, .turn .text.md h3, .turn .text.md h4,
  .turn .text.md h5, .turn .text.md h6 {
    margin: 1.1rem 0 0.4rem; line-height: 1.5;
  }
  .turn .text.md h2 { font-size: 1.05rem; }
  .turn .text.md h3 { font-size: 0.98rem; }
  .turn .text.md h4, .turn .text.md h5, .turn .text.md h6 { font-size: 0.92rem; }
  .turn .text.md ul, .turn .text.md ol { margin: 0.6rem 0; padding-left: 1.4rem; }
  .turn .text.md li { margin: 0.2rem 0; }
  .turn .text.md blockquote {
    margin: 0.6rem 0; padding: 0.1rem 0.9rem; border-left: 3px solid #e3e0da; color: #6b665e;
  }
  .turn .text.md code {
    background: #f1efea; border-radius: 4px; padding: 0.1rem 0.3rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.88em;
  }
  /* コードブロックは**横に流す**(折り返すとインデントが崩れて読めない) */
  .turn .text.md pre {
    margin: 0.7rem 0; padding: 0.7rem 0.9rem; background: #f1efea; border-radius: 8px;
    overflow-x: auto;
  }
  .turn .text.md pre code { background: none; padding: 0; font-size: 0.85em; line-height: 1.6; }
  .turn .text.md a { color: #5560E0; }
  .turn .text.md hr { margin: 1rem 0; border: 0; border-top: 1px solid #e3e0da; }
  /* 表は画面幅を超えることがあるので、はみ出すぶんは横スクロールにする */
  .turn .text.md table {
    display: block; overflow-x: auto; border-collapse: collapse; margin: 0.7rem 0;
    font-size: 0.9rem;
  }
  .turn .text.md th, .turn .text.md td {
    border: 1px solid #e3e0da; padding: 0.35rem 0.6rem; text-align: left;
  }
  .turn .text.md th { background: #f6f4f1; }
  .turn .text.pending { color: #8a857c; }
  .turn .steps { margin: 0.4rem 0 0; font-size: 0.8rem; color: #6b665e; }
  .turn .steps summary { font-weight: normal; color: #6b665e; }
  .turn .steps ol { margin: 0.3rem 0 0; padding-left: 1.2rem;
                    font-family: ui-monospace, monospace; word-break: break-all; }
  .turn .refs { display: flex; flex-wrap: wrap; gap: 0.35rem; margin-top: 0.6rem; }
  .turn .refs a { font-size: 0.78rem; color: #4a463f; text-decoration: none;
                  background: #fff; border: 1px solid #e3e0da; border-radius: 999px;
                  padding: 0.2rem 0.7rem; max-width: 20rem; overflow: hidden;
                  text-overflow: ellipsis; white-space: nowrap; }
  .turn .refs a:hover { border-color: #5560E0; color: #5560E0; }

  /* 入力欄。高さを持たせ(既定 3 行)、設定はその下に並べる */
  .composer { position: sticky; bottom: 0; background: #faf9f7; padding-bottom: 1rem; }
  .composer-box { display: flex; align-items: flex-end; gap: 0.5rem; background: #fff;
                  border: 1px solid #e3e0da; border-radius: 18px; padding: 0.6rem 0.6rem 0.6rem 0.9rem;
                  box-shadow: 0 1px 2px rgba(0,0,0,0.04); }
  .composer-box:focus-within { border-color: #5560E0; box-shadow: 0 0 0 3px #5560E01a; }
  .composer-box textarea { flex: 1; border: none; outline: none; resize: none; background: none;
                           font: inherit; line-height: 1.6; max-height: 40vh; padding: 0.2rem 0; }
  .composer-box button { border: none; border-radius: 50%; width: 2.1rem; height: 2.1rem;
                         background: #5560E0; color: #fff; font-size: 1rem; cursor: pointer;
                         flex: none; padding: 0; }
  .composer-box button:disabled { background: #cdcbc6; cursor: default; }
  .composer-settings { display: flex; flex-wrap: wrap; align-items: center; gap: 0.4rem;
                       margin-top: 0.5rem; font-size: 0.8rem; color: #6b665e; }
  .composer-settings select { border: 1px solid #e3e0da; border-radius: 999px; background: #fff;
                              padding: 0.25rem 0.6rem; font-size: 0.8rem; color: #4a463f; }
  /* **display を指定した要素は hidden 属性が効かない**（作者側の指定が UA の
     display:none に勝つ）。効かない操作を隠しているつもりで**押せてしまう**ので、
     ここで打ち消しておく —— 実際に本番で「覚える」が切れる状態のまま出ていた。 */
  .composer-settings [hidden] { display: none !important; }
  .composer-settings .toggle { display: inline-flex; align-items: center; gap: 0.3rem;
                               border: 1px solid #e3e0da; border-radius: 999px; background: #fff;
                               padding: 0.25rem 0.7rem; cursor: pointer; user-select: none; }
  .composer-settings .toggle.on { border-color: #5560E0; color: #5560E0; background: #f4f5ff; }
  /* 切れないもの（CLI ブリッジの「覚える」）。入っていることは見せ、押せないことも見せる。 */
  .composer-settings .toggle.fixed { cursor: default; opacity: 0.75; }
  .composer-settings .toggle input { margin: 0; }
  .composer-settings .hint { margin-left: auto; }
  @media (max-width: 30rem) {
    .composer-settings { font-size: 0.75rem; }
    .composer-settings .hint { display: none; }
  }
"""


def page_shell(title: str, body: str, refresh: int | None = None, style: str = "") -> str:
    """共通の外枠。`style` は画面ごとの上乗せ(会話画面だけが使う)。

    `title` はページ名だけを渡す(「Chiezo — 」は付けない)。タブの表記は他のプロダクトと
    そろえて「Chiezo — <ページ名>」で統一し、空ならアプリの正式名「AI知識ベース」を出す。
    """
    tab_title = f"Chiezo — {title or 'AI知識ベース'}"
    refresh_tag = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresh_tag}
<link rel="icon" href="{FAVICON_DATA_URI}">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<title>{html.escape(tab_title)}</title>
<style>{PAGE_STYLE}{style}</style>
</head>
<body>
{body}
</body>
</html>"""


def esc(value) -> str:
    return html.escape(str(value)) if value is not None else ""
