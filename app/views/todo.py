"""管理画面の「ToDo」の面(`/admin/todo`)。

タスクとルールを**1 画面に**出す。もとは Vue の SPA(`tasks-frontend/`)を別に持ち、
外へ公開する面(`chiezo-tasks`)だけが Google のログインで守る作りだった。やめた理由は 2 つ:

- **Chiezo は安全なネットワークの中からしか触らせない**、と決めた。外に出す面が
  無くなれば、認証・CSRF・レート制限・リダイレクト URI の許可リストを持つ理由も、
  それらを別コンテナで動かす理由も残らない
- **画面の作りが 2 系統あった。** 管理画面はサーバーで組んだ HTML、やること層は
  ビルドの要る SPA。同じサーバーの中で操作の見た目も配り方も違い、直すたびに
  どちらの流儀かを思い出す必要があった

そこで**管理画面の 1 面に畳んだ**。持ち込まなかったのは、メモの一覧(短期記憶は
記憶の面とブラウズ画面で読める)と見比べ(`/admin/media` に同じものがある)。

この面の決めごと:

- **1 画面に全部出す。** タスク・完了・プロジェクト・ルール・書き出しを縦に並べ、
  行き来を頭の目次(`.todo-index`)だけで済ませる。タスクとルールは同じ日に触るもので、
  画面を分けると「ルールを直してからタスクを流す」のたびに読み込み直しになる
- **JS はインラインの `confirm` まで**(管理画面の流儀)。開閉は `<details>`、
  並び替えはドラッグではなく ↑↓ のフォーム。取り消せない操作にだけ確認を出す
- **書いたら押した場所へ戻す**(303 + `#アンカー`)。断られた理由は `?error=` で
  帯に出す —— ドメイン層(`app/tasks.py`)は `HTTPException` を投げるので、
  そのまま通すと画面の代わりに JSON が出る
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import notes, tasks
from app.jst import JST
from app.jst import format as format_jst
from app.jst import parse as parse_jst
from app.pages import doc_url, esc, page_shell

router = APIRouter()

# 面の居場所。**`/tasks` から移した**(もとは SPA の置き場)。
PATH = "/admin/todo"

# 完了タスクの 1 ページ。溜まる一方のものなので頭打ちにする(遡るのは頁送りで)。
DONE_PAGE_SIZE = 20


# ---- Claude Code へ渡す ------------------------------------------------------
#
# タスクをそのまま Claude Code の入力に流し込むリンク。**やること層の主目的がこれ**で、
# 画面から消すと「書いたタスクを人が手で貼り直す」に戻る。もとは
# `tasks-frontend/src/lib/claudeCode.ts` にあったものを、そのまま写してある。

# web 版 Claude Code のプリフィルの上限は約 5,000 文字。余裕を見て手前で切る
PROMPT_LIMIT = 4500

_GITHUB_URL = re.compile(
    r"^(?:https?://(?:www\.)?|git@)github\.com[/:]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_OWNER_REPO = re.compile(r"^([^/\s:]+)/([^/\s]+?)(?:\.git)?$")


def github_slug(url: str) -> str | None:
    """GitHub のリポジトリ URL を `owner/repo` にする。GitHub 以外は None。"""
    m = _GITHUB_URL.match((url or "").strip())
    return f"{m[1]}/{m[2]}" if m else None


def repo_slug(value: str) -> str | None:
    """URL でも `owner/repo` でも受けて `owner/repo` に揃える。どちらでもなければ None。"""
    if slug := github_slug(value):
        return slug
    m = _OWNER_REPO.match((value or "").strip())
    return f"{m[1]}/{m[2]}" if m else None


def claude_code_url(task: tasks.Task, repo_urls: list[str], rules_repo: str | None) -> str:
    """タスクをプリフィルした Claude Code の URL。

    **タスク番号は渡さない** —— 向こうからこの画面を参照できないので、番号を渡しても
    使い道が無い。代わりに規約リポジトリが設定されていれば「まず共通ルールに従う」の
    一言を先頭に添える(セッションに含めるだけでは、他リポジトリの説明と誤読されうる)。
    """
    rules = repo_slug(rules_repo) if rules_repo else None
    body = tasks.body_rest(task)
    lines = [task.title, "", body] if body else [task.title]
    if rules:
        lines = [
            f"まず、セッションに含まれる規約リポジトリ {rules} の CLAUDE.md(共通ルール)に"
            "従ってください。",
            "",
            *lines,
        ]
    prompt = "\n".join(lines)
    if len(prompt) > PROMPT_LIMIT:
        prompt = f"{prompt[:PROMPT_LIMIT]}\n…(以下略。全文は ToDo の画面で)"
    params = {"prompt": prompt}
    slugs = [s for s in (github_slug(u) for u in repo_urls) if s]
    if rules and rules not in slugs:
        slugs.append(rules)
    if slugs:
        params["repositories"] = ",".join(slugs)
    return "https://claude.ai/code?" + urlencode(params)


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
    url = PATH + (f"?{urlencode(query)}" if query else "") + (f"#{anchor}" if anchor else "")
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


def _when(raw: str) -> str:
    at = parse_jst(raw)
    return format_jst(at) if at else (raw or "")


def _banner(request: Request) -> str:
    q = request.query_params
    if error := q.get("error"):
        return f'<p class="stale">⚠️ {esc(error)}</p>'
    if notice := q.get("notice"):
        return f'<p class="note">✅ {esc(notice)}</p>'
    return ""


def _confirm(action: str, label: str, ask: str) -> str:
    """取り消せない操作のボタン 1 つぶん(押す前に一度訊く)。

    **訊く文にタスクの題名が入る。** 題名は人が自由に書くので `'` が混ざりうるが、
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


def _project_options(projects: list[tasks.Project], selected: str | None) -> str:
    """プロジェクトの選択肢。先頭は未分類(紐づけを外す口を兼ねる)。"""
    options = [
        f'<option value=""{" selected" if not selected else ""}>(未分類)</option>'
    ]
    options += [
        f'<option value="{esc(p.name)}"{" selected" if p.name == selected else ""}>'
        f"{esc(p.name)}</option>"
        for p in projects
    ]
    return "".join(options)


def _status_cell(task: tasks.Task) -> str:
    """状態の切り替え。**いまの状態はボタンにしない**(押しても変わらないため)。"""
    cells = []
    for value, label in (
        (tasks.STATUS_TODO, "未着手"),
        (tasks.STATUS_IN_PROGRESS, "着手中"),
        (tasks.STATUS_DONE, "完了"),
    ):
        if task.status == value:
            cells.append(f'<span class="todo-now">{label}</span>')
            continue
        cells.append(
            f'<form class="todo-inline" method="post" action="{PATH}/tasks/{task.doc_id}/status">'
            f'<input type="hidden" name="status" value="{value}">'
            f'<button type="submit">{label}</button></form>'
        )
    return f'<div class="todo-actions">{"".join(cells)}</div>'


def _flag_cell(task: tasks.Task) -> str:
    """「直すのが大変そう」の印。状態とは別の軸なので、状態の隣に独立して置く。"""
    return (
        f'<form class="todo-inline" method="post" action="{PATH}/tasks/{task.doc_id}/flag">'
        f'<input type="hidden" name="flagged" value="{0 if task.flagged else 1}">'
        f'<button type="submit">{"難所を外す" if task.flagged else "難所にする"}</button>'
        "</form>"
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


def _task_row(
    task: tasks.Task,
    projects: list[tasks.Project],
    repo_urls: list[str],
    rules_repo: str | None,
    first: bool,
    last: bool,
) -> str:
    """タスク 1 行。**開くと編集できる** —— 読むだけの行と編集の画面を分けない。"""
    handoff = (
        f'<a class="todo-handoff" href="{esc(claude_code_url(task, repo_urls, rules_repo))}"'
        ' target="_blank" rel="noopener noreferrer"'
        ' title="Claude Code で開く(内容をプリフィル)">✳</a>'
    )
    flag = '<span class="todo-flag" title="難所">⚑</span>' if task.flagged else ""
    body = tasks.body_rest(task)
    edit = f"""
<form class="todo-edit" method="post" action="{PATH}/tasks/{task.doc_id}">
  <label>タイトル<input type="text" name="title" value="{esc(task.title)}" required></label>
  <label>本文<textarea name="body" rows="6">{esc(body)}</textarea></label>
  <label>プロジェクト<select name="project">{_project_options(projects, task.project)}</select></label>
  <div class="todo-actions">
    <button type="submit">保存</button>
    {_flag_cell(task)}
    {_confirm(f"{PATH}/tasks/{task.doc_id}/delete", "削除",
              f"「{task.title}」を消します。取り消せません。")}
  </div>
</form>
<p class="muted">作成 {esc(_when(task.created_at))} / 更新 {esc(_when(task.updated_at))}
 / <a href="{esc(doc_url(notes.SOURCE_NAME, task.doc_id))}">元のメモ</a></p>
"""
    return f"""<tr>
<td><details><summary>{flag}{esc(task.title)}</summary>{edit}</details></td>
<td>{_status_cell(task)}</td>
<td>{_move_cell(f"{PATH}/tasks/{task.doc_id}/move", first, last)}</td>
<td>{handoff}</td>
</tr>"""


def _group_html(
    heading: str,
    note: str,
    items: list[tasks.Task],
    projects: list[tasks.Project],
    repo_urls: list[str],
    rules_repo: str | None,
) -> str:
    if not items:
        rows = '<p class="muted">このプロジェクトに未完了のタスクはありません。</p>'
    else:
        cells = "".join(
            _task_row(t, projects, repo_urls, rules_repo, i == 0, i == len(items) - 1)
            for i, t in enumerate(items)
        )
        rows = (
            "<table><thead><tr><th>タスク</th><th>状態</th><th>並び</th><th>渡す</th>"
            f"</tr></thead><tbody>{cells}</tbody></table>"
        )
    return f"<h3>{heading}</h3>{note}{rows}"


def _sorted_in_group(items: list[tasks.Task]) -> list[tasks.Task]:
    """グループ内の並び。**手で並べた順 → 作成の新しい順**(`app/tasks.py` と同じ)。

    `tasks.list_tasks()` はプロジェクトを指定したときしか手動の順を見ない
    (番号がプロジェクトをまたぐと意味を成さないため)ので、未分類のかたまりは
    ここで同じ 2 段の並びに揃える。
    """
    ordered = sorted(items, key=lambda t: (t.created_at, t.doc_id), reverse=True)
    ordered.sort(key=lambda t: t.sort_order)
    return ordered


def _tasks_section(
    projects: list[tasks.Project],
    archived: list[tasks.Project],
    rules_repo: str | None,
) -> str:
    active = tasks.list_active_tasks()
    by_project: dict[str | None, list[tasks.Task]] = {None: []}
    for project in projects:
        by_project[project.name] = []
    for task in active:
        by_project.setdefault(task.project, []).append(task)

    def group(project: tasks.Project) -> str:
        note = ""
        if project.archived:
            # **アーカイブは未完了 0 件が条件**なので、ここに来るのは後から
            # 紐づけ直したタスクだけ。出さないと、どの一覧にも並ばずに消える
            note = '<p class="muted">アーカイブ済みですが、未完了のタスクが残っています。</p>'
        if project.description:
            note += f'<p class="muted">{esc(project.description)}</p>'
        if project.repo_urls:
            links = " / ".join(
                f'<a href="{esc(u)}" target="_blank" rel="noopener noreferrer">{esc(u)}</a>'
                for u in project.repo_urls
            )
            note += f'<p class="muted">{links}</p>'
        return _group_html(
            esc(project.name),
            note,
            _sorted_in_group(by_project.get(project.name, [])),
            projects,
            project.repo_urls,
            rules_repo,
        )

    groups = [group(p) for p in projects]
    groups += [group(p) for p in archived if by_project.get(p.name)]
    # 未分類は**末尾に、0 件でも出す** —— 放り込み先であり、プロジェクトを
    # 作る前のタスクが消えたように見えないようにするため
    groups.append(
        _group_html(
            "(未分類)",
            '<p class="muted">プロジェクトに紐づいていないタスク。</p>',
            _sorted_in_group(by_project.get(None, [])),
            projects,
            [],
            rules_repo,
        )
    )

    add = f"""
<form class="todo-entry" method="post" action="{PATH}/tasks">
  <label>タイトル<input type="text" name="title" required placeholder="やること"></label>
  <label>プロジェクト<select name="project">{_project_options(projects, None)}</select></label>
  <label>本文(任意)<textarea name="body" rows="3"></textarea></label>
  <div><button type="submit">足す</button></div>
</form>"""
    return f'<h2 id="tasks">タスク</h2>{add}{"".join(groups)}'


def _done_section(page: int) -> str:
    """完了したもの。**畳んでおく** —— 見に来るのは「あれは片付いたか」を確かめるときだけ。"""
    paged = tasks.list_done_tasks(page=page, size=DONE_PAGE_SIZE)
    total, items = paged["total"], paged["items"]
    if not total:
        inner = '<p class="muted">まだありません。</p>'
    else:
        cells = "".join(
            f"<tr><td>{esc(t.title)}</td>"
            f'<td class="muted">{esc(t.project or "(未分類)")}</td>'
            f'<td class="muted">{esc(_when(t.updated_at))}</td>'
            f'<td><form class="todo-inline" method="post"'
            f' action="{PATH}/tasks/{t.doc_id}/status">'
            f'<input type="hidden" name="status" value="{tasks.STATUS_TODO}">'
            "<button type=\"submit\">戻す</button></form>"
            f'{_confirm(f"{PATH}/tasks/{t.doc_id}/delete", "削除", f"「{t.title}」を消します。取り消せません。")}'
            "</td></tr>"
            for t in items
        )
        pages = (total + DONE_PAGE_SIZE - 1) // DONE_PAGE_SIZE
        pager = []
        if page > 0:
            pager.append(f'<a href="{PATH}?done_page={page - 1}#done">← 新しい</a>')
        pager.append(f'<span class="muted">{page + 1} / {pages} 頁({total} 件)</span>')
        if page + 1 < pages:
            pager.append(f'<a href="{PATH}?done_page={page + 1}#done">古い →</a>')
        inner = (
            "<table><thead><tr><th>タスク</th><th>プロジェクト</th><th>完了</th><th></th>"
            f"</tr></thead><tbody>{cells}</tbody></table>"
            f'<div class="pager">{"".join(pager)}</div>'
        )
    open_attr = " open" if page > 0 else ""
    return (
        f'<h2 id="done">完了</h2><details{open_attr}>'
        f"<summary>片付いたもの({total} 件)</summary>{inner}</details>"
    )


def _projects_section() -> str:
    """プロジェクト(タスクの入れ物)。名前がそのままタスクのタグになる。"""
    rows = []
    # **並びは保存されているとおり**(アーカイブを後ろへ寄せない)。
    # ↑↓ は `reorder_projects` に全件を渡すので、画面の並びが台帳の並びと
    # 違うと、押した行が思ったところへ行かない
    every = tasks.list_projects()
    for index, project in enumerate(every):
        count = len(tasks.list_active_tasks(project.name))
        repos = esc("\n".join(project.repo_urls))
        edit = f"""
<form class="todo-edit" method="post" action="{PATH}/projects/{project.id}">
  <label>名前<input type="text" name="name" value="{esc(project.name)}" required></label>
  <label>説明<input type="text" name="description" value="{esc(project.description)}"></label>
  <label>リポジトリ(1 行に 1 つ)
    <textarea name="repo_urls" rows="3">{repos}</textarea></label>
  <div><button type="submit">保存</button></div>
</form>"""
        if project.archived:
            toggle = (
                f'<form class="todo-inline" method="post"'
                f' action="{PATH}/projects/{project.id}/archive">'
                '<input type="hidden" name="archived" value="0">'
                "<button type=\"submit\">戻す</button></form>"
                + _confirm(
                    f"{PATH}/projects/{project.id}/delete",
                    "削除",
                    f"{project.name} を、紐づくタスクごと消します。取り消せません。",
                )
            )
        else:
            toggle = (
                f'<form class="todo-inline" method="post"'
                f' action="{PATH}/projects/{project.id}/archive">'
                '<input type="hidden" name="archived" value="1">'
                "<button type=\"submit\">アーカイブ</button></form>"
            )
        rows.append(
            f"<tr><td><details><summary>{esc(project.name)}</summary>{edit}</details></td>"
            f'<td class="muted">{esc(project.description)}</td>'
            f"<td>{count} 件</td>"
            f'<td>{"アーカイブ済み" if project.archived else "使用中"}</td>'
            f'<td>{_move_cell(f"{PATH}/projects/{project.id}/move", index == 0, index == len(every) - 1)}</td>'
            f'<td><div class="todo-actions">{toggle}</div></td></tr>'
        )
    table = (
        "<table><thead><tr><th>名前</th><th>説明</th><th>未完了</th><th>状態</th>"
        f'<th>並び</th><th></th></tr></thead><tbody>{"".join(rows)}</tbody></table>'
        if rows
        else '<p class="muted">まだありません。</p>'
    )
    add = f"""
<form class="todo-entry" method="post" action="{PATH}/projects">
  <label>名前<input type="text" name="name" required placeholder="リポジトリ名"></label>
  <label>説明(任意)<input type="text" name="description"></label>
  <label>リポジトリ(1 行に 1 つ・任意)<textarea name="repo_urls" rows="2"></textarea></label>
  <div><button type="submit">作る</button></div>
</form>"""
    return f"""<h2 id="projects">プロジェクト</h2>
<p class="muted">タスクの入れ物。<strong>名前がそのままタスクのタグ</strong>になるので、
名前を変えると紐づくタスクのタグも付け替わります。アーカイブできるのは未完了が 0 件のときだけ、
削除できるのはアーカイブしたものだけです。</p>
{table}{add}"""


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
<p class="muted">ここを設定すると、タスクの ✳(Claude Code へ渡す)が
このリポジトリをセッションに含め、<strong>まず共通ルールに従う</strong>の一言を先頭に添えます。</p>"""
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
    return f"""<h2 id="rules">ルール</h2>
<p class="muted">Claude Code に守らせる共通ルール。
<strong>有効なものだけ</strong>が連結の対象です。</p>
{repo_form}{table}{add}{combined_html}{import_form}"""


def _backup_section(preview: str) -> str:
    """書き出しと取り込み。**書き出したものがそのまま取り込みの入力**になる。"""
    return f"""<h2 id="backup">書き出しと取り込み</h2>
<p class="muted"><strong>未完了のタスク</strong>と、その入れ物(プロジェクトの名前・説明・
リポジトリ)を JSON で持ち出します。完了タスク・並び順・アーカイブ状態は入りません ——
戻したいのは待ち行列であって、画面の状態ではないためです。</p>
<p><a href="{PATH}/export">→ JSON を書き出す</a></p>
<details><summary>取り込む(書き出した JSON を戻す)</summary>
<p class="muted">同じものを二度読み込んでも増えません((プロジェクト名, タイトル)で
照合して飛ばします)。まず「下見」で何が入るかを確かめてください。</p>
<form class="todo-entry" method="post" action="{PATH}/import">
  <label>JSON<textarea name="payload" rows="10" required></textarea></label>
  <div><button type="submit" name="confirm" value="">下見</button></div>
</form>
{preview}
</details>"""


TODO_STYLE = """
  /* ToDo の面(`views/todo.py`)。**行を低く保つ** —— タスクもルールも溜まるもので、
     1 行が本文の高さになると一覧として読めなくなる。中身は `<details>` の中。 */
  .todo-index { display: flex; flex-wrap: wrap; gap: .3rem 1rem; font-size: .9rem;
                margin: .6rem 0 1.2rem; }
  /* 表の中に置くフォームは、行の高さを増やさないように並べる */
  form.todo-inline { display: inline; }
  .todo-actions { display: flex; flex-wrap: wrap; gap: .3rem; align-items: baseline; }
  .todo-actions button { font-size: .8rem; padding: .15rem .5rem; white-space: nowrap; }
  /* いまの状態。**ボタンにしない**(押しても変わらない)ので、字面で見せる */
  .todo-now { font-size: .8rem; font-weight: 700; padding: .15rem .5rem;
              border: 1px solid #5560E0; border-radius: .3rem; color: #333; white-space: nowrap; }
  /* 取り消せない操作だけ色を変える。**同じ見た目のボタンに混ぜない** */
  .todo-danger { color: #b42318; }
  /* 難所の印。行の頭に出して、開かなくても見分けが付くようにする */
  .todo-flag { color: #b9791f; margin-right: .35rem; }
  .todo-handoff { font-size: 1.1rem; text-decoration: none; }
  /* 入力の並び。**器いっぱいには伸ばさない** —— 長い 1 行の入力欄は読みづらい */
  .todo-entry, .todo-edit { display: flex; flex-direction: column; gap: .5rem;
                            max-width: 48rem; margin: .8rem 0; }
  .todo-entry label, .todo-edit label { display: flex; flex-direction: column; gap: .2rem;
                                        font-size: .85rem; color: #444; }
  .todo-entry input[type=text], .todo-edit input[type=text],
  .todo-entry textarea, .todo-edit textarea, .todo-entry select, .todo-edit select {
    font: inherit; font-size: .9rem; padding: .3rem .4rem;
    border: 1px solid #ccc; border-radius: .3rem; }
  .todo-entry textarea, .todo-edit textarea { resize: vertical; }
  .todo-check { flex-direction: row !important; align-items: center; gap: .4rem !important; }
  /* 連結したルール。**読ませるためではなく、選んでコピーするため**に置く */
  .todo-combined { width: 100%; font-family: monospace; font-size: .8rem;
                   border: 1px solid #e5e2dc; background: #f7f6f3; }
  .note { color: #2f6f3e; }
"""


def _disabled_page() -> HTMLResponse:
    from app.views.admin import nav_html

    body = f"""
{nav_html(PATH)}
<h1>ToDo</h1>
<p class="muted">短期記憶が無効なので、タスクもルールも置けません。
書き込み可能なディレクトリを <code>CHIEZO_NOTES_DIR</code>
(または <code>CHIEZO_STATE_DIR</code>)に設定すると使えるようになります。</p>
"""
    return HTMLResponse(content=page_shell("ToDo", body, style=TODO_STYLE))


def _render(
    request: Request,
    *,
    done_page: int = 0,
    rules_preview: str = "",
    backup_preview: str = "",
) -> HTMLResponse:
    from app.views.admin import nav_html

    if not notes.is_enabled():
        return _disabled_page()

    projects = tasks.list_projects(archived=False)
    archived = tasks.list_projects(archived=True)
    rules_repo = tasks.rules_repo_url()
    body = f"""
{nav_html(PATH)}
<h1>ToDo</h1>
<p class="muted">Claude Code に頼みたいことと、守らせたい共通ルールを 1 か所で持つところ。
<strong>専用のテーブルは持たず</strong>、短期記憶(<code>{esc(notes.SOURCE_NAME)}</code>)の
メモにタグで載せてあります。</p>
<nav class="todo-index">
  <a href="#tasks">タスク</a><a href="#done">完了</a><a href="#projects">プロジェクト</a>
  <a href="#rules">ルール</a><a href="#backup">書き出しと取り込み</a>
</nav>
{_banner(request)}
{_tasks_section(projects, archived, rules_repo)}
{_done_section(done_page)}
{_projects_section()}
{_rules_section(rules_repo, rules_preview)}
{_backup_section(backup_preview)}
"""
    return HTMLResponse(content=page_shell("ToDo", body, style=TODO_STYLE))


# ---- 読む --------------------------------------------------------------------


@router.get(PATH, response_class=HTMLResponse)
def todo(request: Request, done_page: int = Query(0, ge=0)):
    """タスクとルールの 1 画面。"""
    return _render(request, done_page=done_page)


@router.get("/tasks", include_in_schema=False)
@router.get("/tasks/{path:path}", include_in_schema=False)
def tasks_moved(path: str = ""):
    """昔の置き場(SPA の `/tasks`)。**ブックマークを迷子にしない**ためだけに残す。"""
    return RedirectResponse(url=PATH, status_code=308)


@router.get(f"{PATH}/export")
def export():
    """未完了のタスクを JSON で書き出す。**そのまま取り込みの入力になる**。"""
    if not notes.is_enabled():
        raise HTTPException(503, {"error": "短期記憶が無効です"})
    payload = tasks.export_tasks()
    # 人が開くファイルなので、名前の日付は JST で数える(UTC だと朝 9 時まで前日になる)
    name = f"chiezo-todo-{datetime.now(JST):%Y%m%d-%H%M}.json"
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# ---- タスク ------------------------------------------------------------------


@router.post(f"{PATH}/tasks")
def create_task(
    title: str = Form(""),
    body: str = Form(""),
    project: str = Form(""),
):
    return _run(
        "tasks",
        lambda: (
            tasks.create_task(title, body or None, project or None),
            f"タスクを足しました: {title.strip()}",
        )[1],
    )


@router.post(f"{PATH}/tasks/{{doc_id}}")
def update_task(
    doc_id: int,
    title: str = Form(""),
    body: str = Form(""),
    project: str = Form(""),
):
    def action() -> str:
        tasks.update_task(
            doc_id,
            title=title,
            body=body,
            project=project or None,
            unlink_project=not project,
        )
        return "保存しました。"

    return _run("tasks", action)


@router.post(f"{PATH}/tasks/{{doc_id}}/status")
def set_task_status(doc_id: int, status: str = Form(...)):
    return _run(
        "tasks",
        lambda: (tasks.update_task(doc_id, status=status), "状態を変えました。")[1],
    )


@router.post(f"{PATH}/tasks/{{doc_id}}/flag")
def set_task_flag(doc_id: int, flagged: str = Form("0")):
    return _run(
        "tasks",
        lambda: (tasks.update_task(doc_id, flagged=flagged == "1"), "印を変えました。")[1],
    )


@router.post(f"{PATH}/tasks/{{doc_id}}/move")
def move_task(doc_id: int, dir: str = Form("up")):
    def action() -> str:
        task = tasks.require_task(doc_id)
        group = [
            t
            for t in tasks.list_active_tasks()
            if t.project == task.project
        ]
        ids = _moved([t.doc_id for t in _sorted_in_group(group)], doc_id, dir)
        tasks.reorder_tasks(task.project, ids)
        return "並びを変えました。"

    return _run("tasks", action)


@router.post(f"{PATH}/tasks/{{doc_id}}/delete")
def delete_task(doc_id: int):
    return _run("tasks", lambda: (tasks.delete_task(doc_id), "消しました。")[1])


# ---- プロジェクト ------------------------------------------------------------


def _repo_lines(raw: str) -> list[str]:
    """1 行 1 つのリポジトリ。空行は捨てる(重複は `app/tasks.py` が落とす)。"""
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]


@router.post(f"{PATH}/projects")
def create_project(
    name: str = Form(""),
    description: str = Form(""),
    repo_urls: str = Form(""),
):
    return _run(
        "projects",
        lambda: (
            tasks.create_project(name, description, _repo_lines(repo_urls)),
            f"プロジェクトを作りました: {name.strip()}",
        )[1],
    )


@router.post(f"{PATH}/projects/{{project_id}}")
def update_project(
    project_id: int,
    name: str = Form(""),
    description: str = Form(""),
    repo_urls: str = Form(""),
):
    return _run(
        "projects",
        lambda: (
            tasks.update_project(project_id, name, description, _repo_lines(repo_urls)),
            "保存しました。",
        )[1],
    )


@router.post(f"{PATH}/projects/{{project_id}}/archive")
def archive_project(project_id: int, archived: str = Form("1")):
    return _run(
        "projects",
        lambda: (
            tasks.update_project(project_id, archived=archived == "1"),
            "アーカイブしました。" if archived == "1" else "戻しました。",
        )[1],
    )


@router.post(f"{PATH}/projects/{{project_id}}/move")
def move_project(project_id: int, dir: str = Form("up")):
    def action() -> str:
        ids = _moved([p.id for p in tasks.list_projects()], project_id, dir)
        tasks.reorder_projects(ids)
        return "並びを変えました。"

    return _run("projects", action)


@router.post(f"{PATH}/projects/{{project_id}}/delete")
def delete_project(project_id: int):
    return _run(
        "projects",
        lambda: (tasks.delete_project(project_id), "消しました(紐づくタスクも一緒に)。")[1],
    )


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


# ---- 書き出しと取り込み ------------------------------------------------------


@router.post(f"{PATH}/import")
def import_tasks(request: Request, payload: str = Form(""), confirm: str = Form("")):
    """書き出した JSON を戻す。**下見を挟む**(何が新しく作られるかを先に見せる)。"""
    try:
        data = json.loads(payload or "")
    except ValueError:
        return _redirect("backup", error="JSON として読めません。書き出したものを貼り付けてください")
    if not isinstance(data, dict):
        return _redirect("backup", error="書き出した形(オブジェクト)ではありません")

    if confirm != "yes":
        try:
            found = tasks.import_tasks(data, dry_run=True)
        except HTTPException as e:
            return _redirect("backup", error=_message(e))
        preview = f"""
<div class="job-status">
<p>新しく作るプロジェクト {len(found["createdProjects"])} 件 /
タスク {len(found["createdTasks"])} 件(既にあるので飛ばすもの
{len(found["skippedTasks"])} 件)。</p>
<form class="todo-inline" method="post" action="{PATH}/import">
  <input type="hidden" name="payload" value="{esc(payload)}">
  <input type="hidden" name="confirm" value="yes">
  <button type="submit">この内容で読み込む</button>
</form>
</div>"""
        return _render(request, backup_preview=preview)

    def action() -> str:
        done = tasks.import_tasks(data)
        return (
            f"プロジェクト {len(done['createdProjects'])} 件、"
            f"タスク {len(done['createdTasks'])} 件を読み込みました"
            f"(飛ばしたタスク {len(done['skippedTasks'])} 件)。"
        )

    return _run("backup", action)
