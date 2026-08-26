"""やること —— タスク・プロジェクト・ルールを `notes` の上にタグで載せる層。

**専用のテーブルも列も持たない。** 種別も状態も所属も「絞り込みの軸」でしかないので、
`notes` の `doc_tags` がそのまま使える。おかげで移植にあたってスキーマの変更が
1 つも要らず、**既に `todo` タグで書かれていたメモが、そのままタスクとして並ぶ**。

対応は次のとおり(語彙の正は `app/notes.py` の `CANONICAL_TAGS`):

| 概念                     | 表現 |
|--------------------------|------|
| タスク                   | tag `todo` の文書 |
| 状態 未着手              | 状態のタグが無い(既定。既存のメモに手を入れずに済む) |
| 状態 着手中 / 完了       | tag `着手中` / `完了` |
| 「直すのが大変そう」の印 | tag `難所`(状態とは別軸) |
| タスクの所属             | プロジェクト名のタグ(リポジトリ名をそのまま使う既存の慣習に乗る) |
| プロジェクト             | tag `project` の文書。見出しが名前、本文が説明 |
| プロジェクトのアーカイブ | tag `アーカイブ` |
| ルール                   | tag `rule` の文書。本文が Markdown |
| ルールの無効             | tag `無効` |

タグで表せないものだけ `docs.extra`(JSON)に置く —— 並び順(`sort_order`)と、
`docs` が持たない作成日時(`created_at`)。並びに作成日時を使うのは、更新のたびに
順番が入れ替わると探しづらいため(`updated_at` は書き換えで動く)。

**書き込みは必ず `app/notes.py` を通す。** `docs` を直接 UPDATE すると FTS と
`doc_tags` / `tag_counts` が本体とずれる。例外は `notes.set_extra()` だけで、
これは `extra` が索引のどれにも関わらないから直接書いてよい。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException

from app import db, notes, settings_store

log = logging.getLogger("chiezo.app")

# ---- タグの語彙(正は notes.CANONICAL_TAGS。ここは参照するための名前) ----------
# タスクを表すタグ。**`todo` とは分けてある** —— あちらは「あとでやる」くらいの
# 意味で普通のメモにも付くので、タスク画面に並べる基準にすると、メモがタスクに
# 化けてしまう。移行は `scripts/migrate_todo_to_task.py`。
TAG_TASK = "task"
TAG_IN_PROGRESS = "着手中"
TAG_DONE = "完了"
TAG_FLAGGED = "難所"
TAG_RULE = "rule"
TAG_RULE_DISABLED = "無効"
TAG_PROJECT = "project"
TAG_ARCHIVED = "アーカイブ"

STATUS_TODO = "todo"
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
STATUSES = (STATUS_TODO, STATUS_IN_PROGRESS, STATUS_DONE)

# 状態 → その状態を表すタグ。未着手はタグを持たない(既定)ので入っていない。
STATUS_TAGS: dict[str, str] = {STATUS_IN_PROGRESS: TAG_IN_PROGRESS, STATUS_DONE: TAG_DONE}
TAG_STATUSES: dict[str, str] = {tag: status for status, tag in STATUS_TAGS.items()}

# 構造を表すタグ。プロジェクト名には使えない —— タスクに付けたときに
# 「所属」ではなく「種別」や「状態」と読まれてしまうため。
STRUCTURAL_TAGS = frozenset({
    TAG_TASK, TAG_IN_PROGRESS, TAG_DONE, TAG_FLAGGED,
    TAG_RULE, TAG_RULE_DISABLED, TAG_PROJECT, TAG_ARCHIVED,
})

# 完了タスクの 1 ページ。cc-tasks の TaskService.listDone と同じ上限。
DONE_PAGE_SIZE_MAX = 100

# 更新で projectId にこれ(0)を送ると紐づけを外す。null は「変更しない」の意味なので、
# 外す指示をそれと分けるための値。doc_id は 1 から振られるので実在の id とぶつからない。
UNLINK_PROJECT_ID = 0


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _bad_request(message: str) -> HTTPException:
    return HTTPException(400, {"error": message})


def _not_found(message: str) -> HTTPException:
    return HTTPException(404, {"error": message})


def _conflict(message: str) -> HTTPException:
    return HTTPException(409, {"error": message})


# ---- 読み出し ---------------------------------------------------------------
#
# 件数が小さい(タスクもルールも人が手で書くもので、多くて数百件)ので、
# タグで絞ってから並べ替えと組み立ては Python 側でやる。SQL に寄せても速くならず、
# 「所属プロジェクトは実在する project 文書の名前と一致するタグ」のような
# 判定を SQL で書くと読めなくなる。

_ROW_COLUMNS = "doc_id, title, body, tags, extra, updated_at"


def _rows_tagged(tag: str) -> list:
    path = notes.require_path()
    notes.ensure_db()
    return db.query(
        path,
        f"SELECT {_ROW_COLUMNS} FROM docs"
        " WHERE doc_id IN (SELECT doc_id FROM doc_tags WHERE tag = ?)",
        (tag,),
    )


def _row(doc_id: int) -> object | None:
    path = notes.require_path()
    notes.ensure_db()
    rows = db.query(path, f"SELECT {_ROW_COLUMNS} FROM docs WHERE doc_id = ?", (doc_id,))
    return rows[0] if rows else None


def _tags_of(row) -> list[str]:
    return json.loads(row["tags"] or "[]")


def _extra_of(row) -> dict:
    return notes.load_extra(row["extra"]) or {}


def _created_at(row) -> str:
    """作成日時。`docs` は持たないので `extra` に置く。

    移植前から `todo` タグで書かれていたメモには入っていないので、
    そのときは `updated_at` に落とす(書いた時刻に一番近い手掛かり)。
    """
    return _extra_of(row).get("created_at") or row["updated_at"]


# ---- タスク -----------------------------------------------------------------

@dataclass(frozen=True)
class Task:
    doc_id: int
    title: str
    body: str
    status: str
    flagged: bool
    project: str | None
    sort_order: int
    created_at: str
    updated_at: str
    tags: list[str]


def _task_of(row, project_names: set[str]) -> Task:
    tags = _tags_of(row)
    extra = _extra_of(row)
    status = STATUS_TODO
    for tag in tags:
        if tag in TAG_STATUSES:
            status = TAG_STATUSES[tag]
            break
    # 所属は「実在する project 文書の名前と一致するタグ」。プロジェクトを作る前から
    # 付いていたタグが、その名前の project を作った瞬間に紐づく(これが狙い)。
    project = next((t for t in tags if t in project_names), None)
    return Task(
        doc_id=row["doc_id"],
        title=row["title"],
        body=row["body"] or "",
        status=status,
        flagged=TAG_FLAGGED in tags,
        project=project,
        sort_order=int(extra.get("sort_order") or 0),
        created_at=_created_at(row),
        updated_at=row["updated_at"],
        tags=tags,
    )


def _project_names() -> set[str]:
    return {row["title"] for row in _rows_tagged(TAG_PROJECT)}


def list_tasks(project: str | None = None, status: str | None = None) -> list[Task]:
    """タスク一覧。`project` / `status` は省略すると絞り込まない。

    並びは「プロジェクト内の手動並び順 → 作成日時の新しい順」。`sort_order` は
    プロジェクト内でしか意味を持たないので、プロジェクトで絞らない全体一覧では見ない
    (番号がプロジェクトをまたいで混ざると探しづらい)。
    """
    if status is not None and status not in STATUSES:
        raise _bad_request(f"status は {' / '.join(STATUSES)} のいずれかを指定してください: {status}")
    names = _project_names()
    tasks = [_task_of(row, names) for row in _rows_tagged(TAG_TASK)]
    if project is not None:
        tasks = [t for t in tasks if t.project == project]
    if status is not None:
        tasks = [t for t in tasks if t.status == status]
    # 昇順と降順が混ざるので 2 段に分ける。Python のソートは安定なので、
    # 先に効かせたい順(作成日時の新しい順)を先に掛けてから、上位のキーで並べ直せばよい。
    tasks.sort(key=lambda t: (t.created_at, t.doc_id), reverse=True)
    if project is not None:
        tasks.sort(key=lambda t: t.sort_order)
    return tasks


def list_active_tasks(project: str | None = None) -> list[Task]:
    """未完了(完了でないもの)。トップの既定表示。"""
    return [t for t in list_tasks(project) if t.status != STATUS_DONE]


def list_done_tasks(project: str | None = None, page: int = 0, size: int = 20) -> dict:
    """完了タスクをページングで返す。

    完了分は「片付いたものの記録」なので手動並び順は見ず、作成日時の新しい順で固定する。
    """
    size = min(max(int(size), 1), DONE_PAGE_SIZE_MAX)
    page = max(int(page), 0)
    done = [t for t in list_tasks(project) if t.status == STATUS_DONE]
    done.sort(key=lambda t: (t.created_at, t.doc_id), reverse=True)
    start = page * size
    return {"items": done[start:start + size], "total": len(done), "page": page, "size": size}


def require_task(doc_id: int) -> Task:
    row = _row(doc_id)
    if row is None or TAG_TASK not in _tags_of(row):
        raise _not_found(f"タスクが見つかりません: doc_id={doc_id}")
    return _task_of(row, _project_names())


def _task_tags(status: str, flagged: bool, project: str | None, keep: list[str]) -> str:
    """タスクのタグを組み立てる。`keep` に渡した「構造でもプロジェクトでもないタグ」は残す。

    メモとして付けた `環境` や `トラブルシュート` を、タスクの操作で落とさないため。
    """
    tags = [TAG_TASK]
    if status in STATUS_TAGS:
        tags.append(STATUS_TAGS[status])
    if flagged:
        tags.append(TAG_FLAGGED)
    if project:
        tags.append(project)
    tags.extend(t for t in keep if t not in tags)
    return ",".join(tags)


def _free_tags(tags: list[str], project_names: set[str]) -> list[str]:
    return [t for t in tags if t not in STRUCTURAL_TAGS and t not in project_names]


def create_task(
    title: str,
    body: str | None = None,
    project: str | None = None,
    status: str | None = None,
    flagged: bool = False,
) -> Task:
    """タスクを 1 件足す。

    `sort_order` は 0(未並び替え)。手で並べた分(1..n)より前に来るので、
    放り込んだタスクはグループの先頭に積まれる。

    `flagged` を渡すのは**取り込み(`import_tasks`)だけ**。印を付けるかは人の判断なので
    放り込む時点では分からないが、書き出したものを戻すときは運ばないと失われる。
    """
    title = (title or "").strip()
    if not title:
        raise _bad_request("title は必須です")
    status = status or STATUS_TODO
    if status not in STATUSES:
        raise _bad_request(f"status は {' / '.join(STATUSES)} のいずれかを指定してください: {status}")
    if project is not None:
        require_project_by_name(project)
    # 本文はタイトルから始める。notes 側はタイトルを本文の 1 行目から作るので、
    # 「タイトルだけのタスク」でも本文が空にならない(notes は空本文を受け付けない)。
    text = title if not body or not body.strip() else f"{title}\n\n{body.strip()}"
    created = notes.add(
        text=text,
        title=title,
        tags=_task_tags(status, flagged, project, []),
        extra={"created_at": _now(), "sort_order": 0},
    )
    return require_task(created["doc_id"])


def update_task(
    doc_id: int,
    title: str | None = None,
    body: str | None = None,
    project: str | None = None,
    status: str | None = None,
    flagged: bool | None = None,
    unlink_project: bool = False,
) -> Task:
    """渡した項目だけを差し替える。状態遷移に制約は設けない(手戻り・中止を許す)。

    `project` は「変更しない(None)」と「紐づけを外す」を分ける必要があるので、
    外すときは `unlink_project=True` を渡す。
    """
    current = require_task(doc_id)
    row = _row(doc_id)
    names = _project_names()

    if title is not None and not title.strip():
        raise _bad_request("title を空にはできません")
    if status is not None and status not in STATUSES:
        raise _bad_request(f"status は {' / '.join(STATUSES)} のいずれかを指定してください: {status}")
    if project is not None and not unlink_project:
        require_project_by_name(project)

    new_title = title.strip() if title is not None else current.title
    new_project = None if unlink_project else (project if project is not None else current.project)
    new_status = status if status is not None else current.status
    new_flagged = flagged if flagged is not None else current.flagged

    new_body = None
    if body is not None or title is not None:
        rest = body.strip() if body is not None else _body_rest(current)
        new_body = f"{new_title}\n\n{rest}" if rest else new_title

    updated = notes.update(
        doc_id,
        text=new_body,
        title=new_title,
        tags=_task_tags(new_status, new_flagged, new_project, _free_tags(_tags_of(row), names)),
    )
    if updated is None:
        raise _not_found(f"タスクが見つかりません: doc_id={doc_id}")
    return require_task(doc_id)


def _body_rest(task: Task) -> str:
    """本文からタイトル行を除いた残り。タイトルだけのタスクなら空。"""
    body = task.body
    if body.startswith(task.title):
        return body[len(task.title):].strip()
    return body.strip()


def reorder_tasks(project: str | None, doc_ids: list[int]) -> list[Task]:
    """プロジェクト内(未紐づけなら未分類のかたまり)の手動並び替え。

    渡した順に 1, 2, 3, … と `sort_order` を振る。画面に出ていないタスクには
    触らないので `doc_ids` は部分集合でよい。ただし別のプロジェクトのタスクを
    混ぜると順序の意味が壊れるので、そこだけは弾く。
    """
    if not doc_ids:
        raise _bad_request("doc_ids は必須です")
    if len(set(doc_ids)) != len(doc_ids):
        raise _bad_request("doc_ids に重複があります")
    for index, doc_id in enumerate(doc_ids, start=1):
        task = require_task(doc_id)
        if task.project != project:
            raise _bad_request(f"別のプロジェクトのタスクは同時に並び替えできません: doc_id={doc_id}")
        row = _row(doc_id)
        extra = _extra_of(row)
        extra["sort_order"] = index
        # 並び替えで updated_at を動かさない(recall の時系列を乱さないため)
        notes.set_extra(doc_id, extra)
    return list_tasks(project)


def delete_task(doc_id: int) -> None:
    require_task(doc_id)
    notes.delete(doc_id)


# ---- プロジェクト -----------------------------------------------------------

@dataclass(frozen=True)
class Project:
    doc_id: int
    name: str
    description: str
    repo_urls: list[str]
    archived: bool
    sort_order: int
    created_at: str
    updated_at: str


def _project_of(row) -> Project:
    extra = _extra_of(row)
    body = (row["body"] or "").strip()
    # 本文はタイトル行から始まる(notes は空本文を許さないので名前を入れてある)
    description = body[len(row["title"]):].strip() if body.startswith(row["title"]) else body
    return Project(
        doc_id=row["doc_id"],
        name=row["title"],
        description=description,
        repo_urls=list(extra.get("repo_urls") or []),
        archived=TAG_ARCHIVED in _tags_of(row),
        sort_order=int(extra.get("sort_order") or 0),
        created_at=_created_at(row),
        updated_at=row["updated_at"],
    )


def list_projects(archived: bool | None = None) -> list[Project]:
    projects = [_project_of(row) for row in _rows_tagged(TAG_PROJECT)]
    if archived is not None:
        projects = [p for p in projects if p.archived == archived]
    projects.sort(key=lambda p: (p.sort_order, p.name))
    return projects


def require_project(doc_id: int) -> Project:
    row = _row(doc_id)
    if row is None or TAG_PROJECT not in _tags_of(row):
        raise _not_found(f"プロジェクトが見つかりません: doc_id={doc_id}")
    return _project_of(row)


def require_project_by_name(name: str) -> Project:
    for project in list_projects():
        if project.name == name:
            return project
    raise _not_found(f"プロジェクトが見つかりません: {name}")


def _require_project_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise _bad_request("name は必須です")
    # 名前はそのままタスクに付くタグになるので、構造タグとぶつかると
    # 「所属」ではなく「種別」や「状態」として読まれてしまう
    if name in STRUCTURAL_TAGS:
        raise _bad_request(f"この名前は構造を表すタグとして使われているので使えません: {name}")
    if name in notes.CANONICAL_TAGS:
        raise _bad_request(f"この名前は定番のタグとして使われているので使えません: {name}")
    return name


def _clean_repo_urls(urls: list[str] | None) -> list[str]:
    """空要素は捨て、重複は先勝ち。"""
    if not urls:
        return []
    cleaned: list[str] = []
    for url in urls:
        url = (url or "").strip()
        if url and url not in cleaned:
            cleaned.append(url)
    return cleaned


def create_project(
    name: str,
    description: str | None = None,
    repo_urls: list[str] | None = None,
) -> Project:
    name = _require_project_name(name)
    if any(p.name == name for p in list_projects()):
        raise _conflict(f"同名のプロジェクトが既にあります: {name}")
    order = max((p.sort_order for p in list_projects()), default=0) + 1
    description = (description or "").strip()
    created = notes.add(
        text=f"{name}\n\n{description}" if description else name,
        title=name,
        tags=TAG_PROJECT,
        extra={
            "created_at": _now(),
            "sort_order": order,
            "repo_urls": _clean_repo_urls(repo_urls),
        },
    )
    return require_project(created["doc_id"])


def update_project(
    doc_id: int,
    name: str | None = None,
    description: str | None = None,
    repo_urls: list[str] | None = None,
    archived: bool | None = None,
) -> Project:
    """渡した項目だけを差し替える。

    アーカイブは**未完了のタスクが 0 件のときだけ**通す。片付いていないタスクごと
    一覧から消えると、放り込んだものを取りこぼすため(戻すのはいつでもよい)。
    """
    current = require_project(doc_id)
    new_name = _require_project_name(name) if name is not None else current.name
    if new_name != current.name and any(p.name == new_name for p in list_projects()):
        raise _conflict(f"同名のプロジェクトが既にあります: {new_name}")

    if archived and not current.archived:
        incomplete = len(list_active_tasks(current.name))
        if incomplete:
            raise _bad_request(
                f"未完了のタスクが {incomplete} 件あるためアーカイブできません: {current.name}"
            )

    new_description = description.strip() if description is not None else current.description
    new_urls = _clean_repo_urls(repo_urls) if repo_urls is not None else current.repo_urls
    new_archived = archived if archived is not None else current.archived

    row = _row(doc_id)
    extra = _extra_of(row)
    extra["repo_urls"] = new_urls
    tags = [TAG_PROJECT] + ([TAG_ARCHIVED] if new_archived else [])

    notes.update(
        doc_id,
        text=f"{new_name}\n\n{new_description}" if new_description else new_name,
        title=new_name,
        tags=",".join(tags),
        extra=extra,
    )
    # 名前はタスク側にタグとして写っているので、変えたら付け替える
    if new_name != current.name:
        _rename_project_tag(current.name, new_name)
    return require_project(doc_id)


def _rename_project_tag(old: str, new: str) -> None:
    """所属タグの付け替え。名前がそのまま紐づけなので、変えたら全タスクを直す。"""
    for row in _rows_tagged(old):
        tags = [new if t == old else t for t in _tags_of(row)]
        notes.update(row["doc_id"], tags=",".join(tags))


def delete_project(doc_id: int) -> None:
    """アーカイブ済みのプロジェクトを、紐づくタスクごと消す。

    アーカイブしていないものは消せない —— アーカイブ自体が「未完了 0 件」を
    条件にしているので、片付いたことを確かめる一段を必ず通させるため。
    """
    current = require_project(doc_id)
    if not current.archived:
        raise _bad_request(f"アーカイブしてからでないと削除できません: {current.name}")
    for task in list_tasks(current.name):
        notes.delete(task.doc_id)
    notes.delete(doc_id)


def reorder_projects(doc_ids: list[int]) -> list[Project]:
    """並び替え。`doc_ids` は全プロジェクト(アーカイブ含む)を望む順で過不足なく。"""
    existing = {p.doc_id for p in list_projects()}
    if not doc_ids or set(doc_ids) != existing or len(set(doc_ids)) != len(doc_ids):
        raise _bad_request("doc_ids には全プロジェクトの doc_id を過不足なく指定してください")
    for index, doc_id in enumerate(doc_ids, start=1):
        extra = _extra_of(_row(doc_id))
        extra["sort_order"] = index
        notes.set_extra(doc_id, extra)
    return list_projects()


# ---- ルール -----------------------------------------------------------------
#
# タスクと違い**本文が主役**なので、本文にタイトル行を混ぜない。
# `combined()` が `## <見出し>` を自分で付けるため、混ぜると見出しが二重になる。

# 連結の先頭に自動で付ける前置き。CLAUDE.md の中身は普通「そのリポジトリ自身の説明」
# として読まれるので、貼り先がどこであれ「作業対象のすべてのリポジトリに効く共通ルール」
# だと明示する。ルールとして登録させないのは、貼り替えのたびに消えたり、
# 並び替えで先頭から動いたりしないようにするため。
COMBINED_PREAMBLE = """# 共通ルール

以下は特定リポジトリの説明ではなく、作業対象のすべてのリポジトリに適用する共通ルール。
"""

# 前置きの直後に自動で付ける「規約リポジトリ自体の扱い」ルール。規約リポジトリは
# 配布専用のプライベートリポジトリで、育てる対象ではない。セッションにサブリポジトリ
# として含まれるため、放っておくとタスクのついでに CLAUDE.md を書き換えられかねない。
COMBINED_REPO_RULE = """## 規約リポジトリの扱い

この CLAUDE.md が置かれているリポジトリ(規約リポジトリ)は、共通ルールを
Claude Code のセッションに読み込ませるための置き場であって、開発対象ではない。
読み取り専用として扱い、**自動では更新しない**(タスクのついでにこの CLAUDE.md を
直したり、このリポジトリへコミット・push したりしない)。
更新するのは、ユーザーが更新後の Markdown を渡して「この内容で規約を更新して」と
明示的に指示したときだけ。そのときは渡された内容で CLAUDE.md を丸ごと置き換える。
"""

# 規約リポジトリ(連結ルールを CLAUDE.md として置く先)の URL。state DB の flags に持つ。
RULES_REPO_URL_KEY = "rules_repo_url"


@dataclass(frozen=True)
class Rule:
    doc_id: int
    title: str
    body: str
    enabled: bool
    sort_order: int
    created_at: str
    updated_at: str


def _rule_of(row) -> Rule:
    return Rule(
        doc_id=row["doc_id"],
        title=row["title"],
        body=(row["body"] or "").strip(),
        enabled=TAG_RULE_DISABLED not in _tags_of(row),
        sort_order=int(_extra_of(row).get("sort_order") or 0),
        created_at=_created_at(row),
        updated_at=row["updated_at"],
    )


def list_rules() -> list[Rule]:
    rules = [_rule_of(row) for row in _rows_tagged(TAG_RULE)]
    rules.sort(key=lambda r: (r.sort_order, r.doc_id))
    return rules


def require_rule(doc_id: int) -> Rule:
    row = _row(doc_id)
    if row is None or TAG_RULE not in _tags_of(row):
        raise _not_found(f"ルールが見つかりません: doc_id={doc_id}")
    return _rule_of(row)


def _rule_tags(enabled: bool) -> str:
    return TAG_RULE if enabled else f"{TAG_RULE},{TAG_RULE_DISABLED}"


def create_rule(title: str, body: str, enabled: bool = True) -> Rule:
    title = (title or "").strip()
    body = (body or "").strip()
    if not title:
        raise _bad_request("title は必須です")
    if not body:
        raise _bad_request("body は必須です")
    order = max((r.sort_order for r in list_rules()), default=0) + 1
    created = notes.add(
        text=body,
        title=title,
        tags=_rule_tags(enabled),
        extra={"created_at": _now(), "sort_order": order},
    )
    return require_rule(created["doc_id"])


def update_rule(
    doc_id: int,
    title: str | None = None,
    body: str | None = None,
    enabled: bool | None = None,
) -> Rule:
    current = require_rule(doc_id)
    if title is not None and not title.strip():
        raise _bad_request("title を空にはできません")
    if body is not None and not body.strip():
        raise _bad_request("body を空にはできません")
    notes.update(
        doc_id,
        text=body.strip() if body is not None else None,
        title=title.strip() if title is not None else current.title,
        tags=_rule_tags(enabled if enabled is not None else current.enabled),
    )
    return require_rule(doc_id)


def delete_rule(doc_id: int) -> None:
    require_rule(doc_id)
    notes.delete(doc_id)


def reorder_rules(doc_ids: list[int]) -> list[Rule]:
    """並び替え。`doc_ids` は全ルールを望む順で過不足なく(プロジェクトと同じ方式)。"""
    existing = {r.doc_id for r in list_rules()}
    if not doc_ids or set(doc_ids) != existing or len(set(doc_ids)) != len(doc_ids):
        raise _bad_request("doc_ids には全ルールの doc_id を過不足なく指定してください")
    for index, doc_id in enumerate(doc_ids, start=1):
        extra = _extra_of(_row(doc_id))
        extra["sort_order"] = index
        notes.set_extra(doc_id, extra)
    return list_rules()


def combined() -> str:
    """有効なルールを表示順に 1 本の Markdown へ連結する。

    各ルールに `## <見出し>` を付けて並べるのは、貼り付け先でどこからどこまでが
    1 ルールかを読み手(と Claude)が判別できるようにするため。
    有効なルールが 1 本も無ければ前置きも付けず空文字を返す。
    """
    body = "\n".join(f"## {r.title}\n\n{r.body}\n" for r in list_rules() if r.enabled)
    if not body:
        return ""
    return f"{COMBINED_PREAMBLE}\n{COMBINED_REPO_RULE}\n{body}"


def _heading_of(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("## "):
            return line[3:].strip()
    raise ValueError(f"見出しのない自動付与ルール: {markdown}")


# 連結時に自動で付くルールの見出し。取り込みでは捨てる。COMBINED_REPO_RULE 自身の
# 1 行目から取る —— 同じ文字列を 2 か所に書くと、片方だけ直したときに
# 黙って二重取り込みになる。
AUTO_ADDED_TITLE = _heading_of(COMBINED_REPO_RULE)


def _fence_length(stripped: str, fence: str) -> int:
    length = 0
    while length < len(stripped) and stripped[length] == fence:
        length += 1
    return length


def parse_rule_markdown(markdown: str | None) -> list[tuple[str, str]]:
    """連結ルールの Markdown を個々のルールへ戻す(`combined()` の逆)。

    - `## <見出し>` で 1 本に区切る。見出しの下から次の見出しの手前までが本文
    - 最初の見出しより前(前置き)は捨てる。連結時に自動で付くものなので、
      ルールとして取り込むと貼り替えのたびに増える
    - 同じ理由で「規約リポジトリの扱い」も捨てる
    - **見出しの判定はコードブロックの外だけ**で行う。ルール本文にはシェルの例が
      入ることがあり、フェンス内の `## …` を見出しと解釈するとそこで分断される
    """
    if not markdown or not markdown.strip():
        return []
    parsed: list[tuple[str, str]] = []
    title: str | None = None
    body: list[str] = []
    fence_char = ""
    fence_length = 0

    def flush() -> None:
        if not title or title == AUTO_ADDED_TITLE:
            return
        text = "\n".join(body).strip()
        if text:
            parsed.append((title, text))

    for line in markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if fence_char:
            if _fence_length(stripped, fence_char) >= fence_length:
                fence_char = ""
        elif _fence_length(stripped, "`") >= 3 or _fence_length(stripped, "~") >= 3:
            fence_char = stripped[0]
            fence_length = _fence_length(stripped, fence_char)
        elif stripped.startswith("## "):
            flush()
            title = stripped[3:].strip()
            body = []
            continue
        if title is not None:
            body.append(line)
    flush()
    return parsed


def import_rules(markdown: str, replace: bool = False, dry_run: bool = False) -> dict:
    """連結ルールの Markdown を貼り付けて、ルール一覧へ戻す。

    ルールを失っても、貼り付け先に残っている 1 本の Markdown から復旧できるようにするため。
    `replace` が真なら既存を全消しして入れ替え、偽なら末尾に足す。
    `dry_run` なら書き込まず、取り込む見出しだけ返す —— 入れ替えは取り消せないので、
    何が入るかを先に見せる。
    """
    parsed = parse_rule_markdown(markdown)
    if not parsed:
        raise _bad_request(
            "取り込めるルールが見つかりません。`## 見出し` で区切った Markdown を貼り付けてください"
        )
    titles = [title for title, _ in parsed]
    if dry_run:
        return {"titles": titles, "rules": []}
    if replace:
        for rule in list_rules():
            notes.delete(rule.doc_id)
    for title, body in parsed:
        create_rule(title, body)
    return {"titles": titles, "rules": list_rules()}


def rules_repo_url() -> str | None:
    """規約リポジトリ。未設定なら None。"""
    return settings_store.get_flag(RULES_REPO_URL_KEY)


def set_rules_repo_url(value: str | None) -> str | None:
    """規約リポジトリの更新。空文字を渡すと消す。更新後の値を返す。"""
    settings_store.set_flag(RULES_REPO_URL_KEY, value)
    return rules_repo_url()


# ---- 持ち出しと取り込み -----------------------------------------------------
#
# DB を失っても打ち直さずに戻せるようにするための機能。**書き出したものをそのまま
# 読み込める**(書き出し結果が読み込みの入力と同じ形)ので、テキストとして手元に
# 置いておけばバックアップになる。
#
# 持ち出すのは未完了のタスクと、その所属プロジェクトの名前・リポジトリだけ。
# 完了タスクは「片付いたものの記録」で復元する意味が薄く、プロジェクトの説明・
# 並び順・アーカイブ状態も対象にしない(戻したいのは待ち行列であって画面の状態ではない)。

# 書き出し形式の版。読み込み側は「これ以下なら読む」で判定する ——
# 将来 2 を書き出すようになっても、古い 1 のファイルは読めるようにするため。
EXPORT_FORMAT_VERSION = 1


def _exported(items: list[Task]) -> list[dict]:
    return [{"title": t.title, "status": t.status, "flagged": t.flagged} for t in items]


def export_tasks() -> dict:
    """未完了タスクを書き出す。プロジェクトは表示順、タスクは各プロジェクト内の並び順。

    未完了タスクを持たないプロジェクトは含めない —— 戻したいのはタスクなので、
    空のプロジェクトまで作ると復元先に使っていない箱が増える。
    """
    projects = []
    for project in list_projects():
        items = list_active_tasks(project.name)
        if items:
            projects.append({
                "name": project.name,
                "repoUrls": project.repo_urls,
                "tasks": _exported(items),
            })
    unassigned = [t for t in list_active_tasks() if t.project is None]
    return {
        "version": EXPORT_FORMAT_VERSION,
        "exportedAt": _now(),
        "projects": projects,
        "unassignedTasks": _exported(unassigned),
    }


def _transfer_key(project_name: str, title: str) -> str:
    # NUL 区切り。表示に使える文字でつなぐと、名前とタイトルの境目が違っても
    # 同じキーになりうる(「A」+「B C」と「A B」+「C」)。
    return f"{project_name}\0{title}"


def import_tasks(data: dict | None, dry_run: bool = False) -> dict:
    """書き出したものを読み込む。

    プロジェクトは**名前で照合し、無ければ作る**(リポジトリも一緒に登録する)。
    既にあるものは触らない —— 復元のたびに手元の設定を上書きされると困る。

    タスクは**同じプロジェクトに同じタイトルの未完了タスクがあれば飛ばす**。
    同じファイルを二度読んでも増えないようにするため(復元は繰り返し試すことがある)。
    """
    data = data or {}
    entries = data.get("projects") or []
    unassigned = data.get("unassignedTasks") or []
    if not entries and not unassigned:
        raise _bad_request("読み込めるタスクがありません。書き出した JSON を貼り付けてください")
    version = int(data.get("version") or EXPORT_FORMAT_VERSION)
    if version > EXPORT_FORMAT_VERSION:
        raise _bad_request(
            f"対応していない形式です(version={version})。"
            f"このアプリが読めるのは {EXPORT_FORMAT_VERSION} までです"
        )

    result: dict[str, list[str]] = {"createdProjects": [], "createdTasks": [], "skippedTasks": []}
    # 既存の未完了タスクの (プロジェクト名, タイトル)。id ではなく名前で持つのは、
    # dry_run ではまだ作っていないプロジェクトがあるため。
    taken = {_transfer_key(t.project or "", t.title) for t in list_active_tasks()}
    existing_names = {p.name for p in list_projects()}

    for entry in entries:
        name = (entry.get("name") or "").strip()
        if not name:
            raise _bad_request("プロジェクト名が空のものがあります")
        if name not in existing_names:
            result["createdProjects"].append(name)
            existing_names.add(name)
            if not dry_run:
                create_project(name, repo_urls=entry.get("repoUrls"))
        _import_into(name, entry.get("tasks"), taken, result, dry_run)
    _import_into("", unassigned, taken, result, dry_run)
    return result


def _import_into(
    project_name: str,
    items: list[dict] | None,
    taken: set[str],
    result: dict[str, list[str]],
    dry_run: bool,
) -> None:
    for item in items or []:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        label = title if not project_name else f"{project_name} / {title}"
        key = _transfer_key(project_name, title)
        # 既にある、または同じファイル内での重複
        if key in taken:
            result["skippedTasks"].append(label)
            continue
        taken.add(key)
        result["createdTasks"].append(label)
        if not dry_run:
            status = item.get("status") or STATUS_TODO
            create_task(
                title,
                project=project_name or None,
                status=status if status in STATUSES else STATUS_TODO,
                flagged=bool(item.get("flagged")),
            )


# ---- そのほかのメモ(どの画面にも載らないもの)-------------------------------
#
# タスク・プロジェクト・ルールは構造タグで表しているので、**そのどれも持たないメモ**が
# ここに落ちる(決定・環境・runbook・トラブルシュート…)。短期記憶に溜まる大半は
# こちらで、画面から見る手段がここまで無かった。

# 画面が持つ 3 つの入れ物。これを持つものは専用の画面にいるので、そのほかからは外す。
CONTAINER_TAGS = (TAG_TASK, TAG_PROJECT, TAG_RULE)

# 1 ページの上限。短期記憶は数十〜数千件なので、深追いせず頭から見る作り。
NOTE_PAGE_SIZE_MAX = 200


@dataclass(frozen=True)
class Note:
    doc_id: int
    title: str
    body: str
    tags: list[str]
    created_at: str
    updated_at: str


def _note_of(row) -> Note:
    return Note(
        doc_id=row["doc_id"],
        title=row["title"],
        body=row["body"] or "",
        tags=_tags_of(row),
        created_at=_created_at(row),
        updated_at=row["updated_at"],
    )


def list_notes(tag: str | None = None, limit: int = 50, offset: int = 0) -> tuple[list[Note], int]:
    """どの画面にも載らないメモを新しい順に。件数も返す。

    上限をここで担保するのは `notes.recall()` と同じ理由で、REST の `Query` は
    HTTP の口にしか効かないため(SQLite は `LIMIT -1` を無制限と解釈する)。
    """
    path = notes.require_path()
    notes.ensure_db()
    limit = max(1, min(int(limit), NOTE_PAGE_SIZE_MAX))
    offset = max(0, int(offset))
    where = [
        "d.doc_id NOT IN (SELECT doc_id FROM doc_tags WHERE tag IN"
        f" ({','.join('?' * len(CONTAINER_TAGS))}))"
    ]
    params: list = list(CONTAINER_TAGS)
    for name in notes.split_tags(tag):
        where.append("d.doc_id IN (SELECT doc_id FROM doc_tags WHERE tag = ?)")
        params.append(name)
    clause = " WHERE " + " AND ".join(where)
    (total,) = db.query(path, f"SELECT COUNT(*) FROM docs d{clause}", tuple(params))[0]
    rows = db.query(
        path,
        f"SELECT {_ROW_COLUMNS} FROM docs d{clause}"
        " ORDER BY d.updated_at DESC, d.doc_id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    return [_note_of(row) for row in rows], total


def note_tags() -> list[tuple[str, int]]:
    """そのほかのメモに付いているタグと件数(多い順)。絞り込みの候補に使う。

    `tag_counts` は短期記憶ぜんぶを数えているので、ここでは使えない ——
    タスクやルールに付いたぶんまで混ざる。
    """
    path = notes.require_path()
    notes.ensure_db()
    rows = db.query(
        path,
        "SELECT dt.tag AS tag, COUNT(*) AS docs FROM doc_tags dt"
        " WHERE dt.doc_id NOT IN (SELECT doc_id FROM doc_tags WHERE tag IN"
        f" ({','.join('?' * len(CONTAINER_TAGS))}))"
        " GROUP BY dt.tag ORDER BY docs DESC, tag",
        tuple(CONTAINER_TAGS),
    )
    return [(row["tag"], row["docs"]) for row in rows]
