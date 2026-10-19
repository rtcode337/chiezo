"""管理画面の「ルール」の節(記憶の面 `/admin/memory` のいちばん下。書き込みの口は `/admin/todo/rules/...`)。

ToDo の層(`app/tasks.py`)のうち、画面に出すのは**ルールだけ**。プロジェクトとタスクは
置き場(短期記憶のタグ・設定の置き場)と REST(`/v1/todo/...`)だけを持ち、**操作は
LAN の中のタスクと予定のアプリに一本化した**。ここにも同じ操作を置くと、同じタスクを
2 か所で直すことになり、どちらの画面が正なのかが読めなくなる。

もとはタスクとルールを Vue の SPA(`tasks-frontend/`)で別に持ち、外へ公開する面
(`chiezo-tasks`)だけが Google のログインで守る作りだった。**Chiezo は安全な
ネットワークの中からしか触らせない**と決めて管理画面に畳み、そのあとタスクの側を外へ出した。

この節の決めごと:

- **JS はインラインの `confirm` まで**(管理画面の流儀)。開閉は `<details>`、
  並び替えはドラッグではなく ↑↓ のフォーム。取り消せない操作にだけ確認を出す
- **書いたら押した場所へ戻す**(303 + `#アンカー`)。断られた理由は `?error=` で
  帯に出す —— ドメイン層(`app/tasks.py`)は `HTTPException` を投げるので、
  そのまま通すと画面の代わりに JSON が出る
"""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import notes, tasks
from app.pages import esc

router = APIRouter()

# 書き込みの口の置き場。**`/tasks` から移した**(もとは SPA の置き場)。
PATH = "/admin/todo"
# 画面の居場所。**記憶の面のいちばん下に節として置く**(`section_html`)。
# ルールは短期記憶のメモにタグで載っているだけなので、記憶の面の中にあれば
# 帯の面を 1 つ減らせる。書いたらここへ戻す
PAGE = "/admin/memory"
# 節の頭の居場所(「ルール」の見出し)
SECTION_ANCHOR = "rules"


# ---- 書き込みの共通部品 ------------------------------------------------------


def _message(exc: HTTPException) -> str:
    """ドメイン層が投げた `HTTPException` から、人に見せる 1 行を取り出す。"""
    detail = exc.detail
    if isinstance(detail, dict):
        return str(detail.get("error") or detail)
    return str(detail)


def _redirect(anchor: str = "", *, error: str = "", notice: str = "") -> RedirectResponse:
    """押した場所へ戻す。**断られた理由も一緒に持って帰る**。"""
    query = {k: v for k, v in (("error", error), ("notice", notice)) if v}
    url = PAGE + (f"?{urlencode(query)}" if query else "") + (f"#{anchor}" if anchor else "")
    return RedirectResponse(url, status_code=303)


def _run(anchor: str, action) -> RedirectResponse:
    """書き込みを 1 つ実行して戻す。断られたら帯に出す(JSON を返さない)。"""
    try:
        notice = action()
    except HTTPException as e:
        return _redirect(anchor, error=_message(e))
    return _redirect(anchor, notice=notice or "")


def _moved(ids: list[int], target: int, direction: str) -> list[int]:
    """`target` を 1 つ上/下へ動かした並び。端なら動かさない。"""
    if target not in ids:
        return ids
    i = ids.index(target)
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
    return ids


def _checked(value: str | None) -> bool:
    """チェックボックスは入っていれば真(値は見ない)。"""
    return value is not None


# ---- 画面の部品 --------------------------------------------------------------


def _banner(request: Request) -> str:
    q = request.query_params
    if error := q.get("error"):
        return f'<p class="stale">⚠️ {esc(error)}</p>'
    if notice := q.get("notice"):
        return f'<p class="note">✅ {esc(notice)}</p>'
    return ""


def _confirm(action: str, label: str, ask: str) -> str:
    """取り消せない操作のボタン 1 つぶん(押す前に一度訊く)。

    **訊く文にルールの見出しが入る。** 見出しは人が自由に書くので `'` が混ざりうるが、
    ブラウザは属性値の実体参照を JS に渡す前に戻すので、`esc()` だけでは
    `onsubmit` の中の文字列が途中で閉じる(そこから先が構文エラーになって、
    押しても何も起きないボタンになる)。先に JS 側の逃がしを入れておく。
    """
    js = ask.replace("\\", "\\\\").replace("'", "\\'")
    return (
        f'<form class="todo-inline" method="post" action="{esc(action)}"'
        f" onsubmit=\"return confirm('{esc(js)}')\">"
        f'<button type="submit" class="todo-danger">{esc(label)}</button></form>'
    )


def _move_cell(action: str, first: bool, last: bool) -> str:
    """↑↓ 1 組。**端では出さない**(押せないボタンを置かない、の流儀)。"""
    up = (
        ""
        if first
        else (
            f'<form class="todo-inline" method="post" action="{esc(action)}">'
            '<input type="hidden" name="dir" value="up">'
            '<button type="submit" title="上へ">↑</button></form>'
        )
    )
    down = (
        ""
        if last
        else (
            f'<form class="todo-inline" method="post" action="{esc(action)}">'
            '<input type="hidden" name="dir" value="down">'
            '<button type="submit" title="下へ">↓</button></form>'
        )
    )
    return f'<div class="todo-actions">{up}{down}</div>'


def _rules_section(rules_repo: str | None, preview: str) -> str:
    """ルール(Claude Code に守らせる共通ルール)。本文が主役なので、開くと全文が出る。"""
    rules = tasks.list_rules()
    rows = []
    for index, rule in enumerate(rules):
        edit = f"""
<form class="todo-edit" method="post" action="{PATH}/rules/{rule.doc_id}">
  <label>見出し<input type="text" name="title" value="{esc(rule.title)}" required></label>
  <label>本文(Markdown)<textarea name="body" rows="12">{esc(rule.body)}</textarea></label>
  <div class="todo-actions">
    <button type="submit">保存</button>
    {_confirm(f"{PATH}/rules/{rule.doc_id}/delete", "削除",
              f"ルール「{rule.title}」を消します。取り消せません。")}
  </div>
</form>"""
        toggle = (
            f'<form class="todo-inline" method="post" action="{PATH}/rules/{rule.doc_id}/toggle">'
            f'<input type="hidden" name="enabled" value="{0 if rule.enabled else 1}">'
            f'<button type="submit">{"無効にする" if rule.enabled else "有効にする"}</button>'
            "</form>"
        )
        rows.append(
            f"<tr><td><details><summary>{esc(rule.title)}</summary>{edit}</details></td>"
            f'<td>{"有効" if rule.enabled else "<span class=\'muted\'>無効</span>"}</td>'
            f'<td>{_move_cell(f"{PATH}/rules/{rule.doc_id}/move", index == 0, index == len(rules) - 1)}</td>'
            f'<td><div class="todo-actions">{toggle}</div></td></tr>'
        )
    table = (
        "<table><thead><tr><th>ルール</th><th>状態</th><th>並び</th><th></th></tr></thead>"
        f'<tbody>{"".join(rows)}</tbody></table>'
        if rows
        else '<p class="muted">まだありません。</p>'
    )
    add = f"""
<form class="todo-entry" method="post" action="{PATH}/rules">
  <label>見出し<input type="text" name="title" required></label>
  <label>本文(Markdown)<textarea name="body" rows="6" required></textarea></label>
  <div><button type="submit">足す</button></div>
</form>"""
    combined = tasks.combined()
    combined_html = (
        f'<details><summary>まとめて表示({len(combined):,} 文字)</summary>'
        f'<p class="muted">有効なルールを 1 本の Markdown に連結したもの。'
        f"規約リポジトリの CLAUDE.md に丸ごと貼り替えて使います。</p>"
        f'<textarea class="todo-combined" rows="16" readonly>{esc(combined)}</textarea></details>'
        if combined
        else '<p class="muted">有効なルールが無いので、連結するものがありません。</p>'
    )
    repo_form = f"""
<form class="todo-entry" method="post" action="{PATH}/rules/repo">
  <label>規約リポジトリ(共通ルールを CLAUDE.md として置く先)
    <input type="text" name="url" value="{esc(rules_repo or "")}"
           placeholder="https://github.com/owner/repo"></label>
  <div><button type="submit">保存</button></div>
</form>
<p class="muted">下の「まとめて表示」を貼り替える先の控えです。連結したルールは
このリポジトリの CLAUDE.md として置き、Claude Code のセッションに含めて読ませます。</p>"""
    import_form = f"""
<details><summary>取り込む(貼り付けたルールを戻す)</summary>
<p class="muted"><code>## 見出し</code> で区切った Markdown を貼り付けます。
まず「下見」で何が入るかを確かめてください。</p>
<form class="todo-entry" method="post" action="{PATH}/rules/import">
  <label>Markdown<textarea name="markdown" rows="10" required></textarea></label>
  <label class="todo-check"><input type="checkbox" name="replace" value="1">
    いまのルールを全部消して入れ替える</label>
  <div><button type="submit" name="confirm" value="">下見</button></div>
</form>
{preview}
</details>"""
    return f"""<h2 id="{SECTION_ANCHOR}">ルール</h2>
<p class="muted">Claude Code に守らせる共通ルール。
<strong>有効なものだけ</strong>が連結の対象です。</p>
{repo_form}{table}{add}{combined_html}{import_form}"""


TODO_STYLE = """
  /* ルールの節(`views/todo.py`)。**行を低く保つ** —— ルールは溜まるもので、
     1 行が本文の高さになると一覧として読めなくなる。中身は `<details>` の中。 */
  /* 表の中に置くフォームは、行の高さを増やさないように並べる */
  form.todo-inline { display: inline; }
  .todo-actions { display: flex; flex-wrap: wrap; gap: .3rem; align-items: baseline; }
  .todo-actions button { font-size: .8rem; padding: .15rem .5rem; white-space: nowrap; }
  /* 取り消せない操作だけ色を変える。**同じ見た目のボタンに混ぜない** */
  .todo-danger { color: #b42318; }
  /* 入力の並び。**器いっぱいには伸ばさない** —— 長い 1 行の入力欄は読みづらい */
  .todo-entry, .todo-edit { display: flex; flex-direction: column; gap: .5rem;
                            max-width: 48rem; margin: .8rem 0; }
  .todo-entry label, .todo-edit label { display: flex; flex-direction: column; gap: .2rem;
                                        font-size: .85rem; color: #444; }
  .todo-entry input[type=text], .todo-edit input[type=text],
  .todo-entry textarea, .todo-edit textarea {
    font: inherit; font-size: .9rem; padding: .3rem .4rem;
    border: 1px solid #ccc; border-radius: .3rem; }
  .todo-entry textarea, .todo-edit textarea { resize: vertical; }
  .todo-check { flex-direction: row !important; align-items: center; gap: .4rem !important; }
  /* 連結したルール。**読ませるためではなく、選んでコピーするため**に置く */
  .todo-combined { width: 100%; font-family: monospace; font-size: .8rem;
                   border: 1px solid #e5e2dc; background: #f7f6f3; }
  .note { color: #2f6f3e; }
"""


def section_html(request: Request, *, rules_preview: str = "") -> str:
    """記憶の面のいちばん下に置くルールの節。

    **短期記憶が無効なら、置けない理由だけ出す**(空にすると、消えたように見える)。
    """
    if not notes.is_enabled():
        return (
            f'<h2 id="{SECTION_ANCHOR}">ルール</h2>'
            '<p class="muted">短期記憶が無効なので、ルールを置けません。'
            "書き込み可能なディレクトリを <code>CHIEZO_NOTES_DIR</code>"
            "(または <code>CHIEZO_STATE_DIR</code>)に設定すると使えるようになります。</p>"
        )
    return f"""{_banner(request)}
{_rules_section(tasks.rules_repo_url(), rules_preview)}
"""


def _render(request: Request, **previews) -> HTMLResponse:
    """取り込みの下見を載せた記憶の面(下見は書き込みの口が組むので、ここから描く)。"""
    from app.views.admin import memory_page

    return memory_page(request, **previews)


# ---- ルール ------------------------------------------------------------------


@router.post(f"{PATH}/rules/repo")
def set_rules_repo(url: str = Form("")):
    return _run(
        "rules",
        lambda: (tasks.set_rules_repo_url(url.strip()), "規約リポジトリを保存しました。")[1],
    )


@router.post(f"{PATH}/rules/import")
def import_rules(
    request: Request,
    markdown: str = Form(""),
    replace: str | None = Form(None),
    confirm: str = Form(""),
):
    """貼り付けた Markdown をルールへ戻す。**下見を挟む**(入れ替えは取り消せない)。"""
    wants_replace = _checked(replace)
    if confirm != "yes":
        try:
            found = tasks.import_rules(markdown, replace=wants_replace, dry_run=True)
        except HTTPException as e:
            return _redirect("rules", error=_message(e))
        titles = "".join(f"<li>{esc(t)}</li>" for t in found["titles"])
        preview = f"""
<div class="job-status">
<p>{len(found["titles"])} 本のルールが入ります{"(いまのルールは全部消えます)" if wants_replace else ""}。</p>
<ul>{titles}</ul>
<form class="todo-inline" method="post" action="{PATH}/rules/import">
  <input type="hidden" name="markdown" value="{esc(markdown)}">
  {'<input type="hidden" name="replace" value="1">' if wants_replace else ""}
  <input type="hidden" name="confirm" value="yes">
  <button type="submit">この内容で取り込む</button>
</form>
</div>"""
        return _render(request, rules_preview=preview)

    def action() -> str:
        done = tasks.import_rules(markdown, replace=wants_replace)
        return f"{len(done['titles'])} 本のルールを取り込みました。"

    return _run("rules", action)


@router.post(f"{PATH}/rules")
def create_rule(title: str = Form(""), body: str = Form("")):
    return _run(
        "rules",
        lambda: (tasks.create_rule(title, body), f"ルールを足しました: {title.strip()}")[1],
    )


@router.post(f"{PATH}/rules/{{doc_id}}")
def update_rule(doc_id: int, title: str = Form(""), body: str = Form("")):
    return _run(
        "rules", lambda: (tasks.update_rule(doc_id, title, body), "保存しました。")[1]
    )


@router.post(f"{PATH}/rules/{{doc_id}}/toggle")
def toggle_rule(doc_id: int, enabled: str = Form("1")):
    return _run(
        "rules",
        lambda: (
            tasks.update_rule(doc_id, enabled=enabled == "1"),
            "有効にしました。" if enabled == "1" else "無効にしました。",
        )[1],
    )


@router.post(f"{PATH}/rules/{{doc_id}}/move")
def move_rule(doc_id: int, dir: str = Form("up")):
    def action() -> str:
        ids = _moved([r.doc_id for r in tasks.list_rules()], doc_id, dir)
        tasks.reorder_rules(ids)
        return "並びを変えました。"

    return _run("rules", action)


@router.post(f"{PATH}/rules/{{doc_id}}/delete")
def delete_rule(doc_id: int):
    return _run("rules", lambda: (tasks.delete_rule(doc_id), "消しました。")[1])
