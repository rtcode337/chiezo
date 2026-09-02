"""やること層の REST(`/api/**`)。cc-tasks から移した画面がそのまま話せる形。

**chiezo 本体の `/v1/...` とは別の面**である。あちらは知識ベースの機械向けの口で、
LAN 内・認証なし。こちらは cc-tasks の PWA が話す相手で、外に出す前提の口
(認証は `app/tasks_app.py` が被せる)。だから次の 2 点で本体と流儀が違う:

- **JSON は camelCase**(要求は snake_case でも受ける)。既存の画面の型定義に合わせる
- **エラーは `{"error": {"code", "message"}}`**。本体は `{"error": "..."}` だが、
  画面側が `error.message` を読んで文言をそのまま出すので、平たくすると
  「未完了のタスクが 3 件あるためアーカイブできません」のような案内が消える

パスの並び順に注意。`/api/tasks/{task_id}` を先に宣言すると `order` / `export` /
`import` が id として解釈されて 422 になるので、**固定のパスを先に置く**。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app import media, tasks

log = logging.getLogger("chiezo.app")

router = APIRouter(prefix="/api")


class _Input(BaseModel):
    """要求の本体。camelCase(画面が送る形)でも snake_case でも受ける。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


def _optional_bool(value: str | None) -> bool | None:
    """三値の真偽をクエリから読む。**空文字は「絞り込まない」**。

    画面は「全件」を `?archived=`(値だけ空)で送る。FastAPI に `bool | None` として
    宣言すると空文字を変換できず 422 になり、一覧がまるごと出なくなる
    (実際に踏んだ。テストでは `?archived=true` しか送っていなかったので気づけなかった)。
    """
    if value is None or value == "":
        return None
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes"}:
        return True
    if lowered in {"0", "false", "no"}:
        return False
    raise HTTPException(400, {"error": f"true か false を指定してください: {value}"})


# ---- エラーの形 -------------------------------------------------------------

_CODES = {400: "bad_request", 404: "not_found", 409: "conflict"}


def _error_body(status: int, detail) -> dict:
    """`app/tasks.py` が投げた `{"error": "..."}` を画面が読む形に直す。"""
    message = detail
    if isinstance(detail, dict):
        message = detail.get("error") or detail.get("message") or str(detail)
    return {"error": {"code": _CODES.get(status, "unknown"), "message": str(message)}}


def install_error_handlers(app, only_under: str | None = None) -> None:
    """やること層のアプリに、画面が読めるエラーの形を仕込む。

    `only_under` を渡すと、そのパスの下だけをこの形にして、外れたものは今までどおりの
    扱いに戻す。本体(`chiezo-app`)にも同じ REST を載せているため —— あちらのエラーは
    `{"error": "..."}` と平たく、この層は `{"error": {"code", "message"}}` で、
    例外ハンドラはアプリ単位でしか差せない。区別せずに差すと本体側のエラーが
    全部この形に変わる(実際に 28 件のテストが落ちた)。
    """
    previous = app.exception_handlers.get(HTTPException)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        if only_under and not request.url.path.startswith(only_under):
            if previous is not None:
                return await previous(request, exc)
            raise exc
        return JSONResponse(status_code=exc.status_code, content=_error_body(exc.status_code, exc.detail))


# ---- 応答の組み立て ---------------------------------------------------------


def _project_ids() -> dict[str, int]:
    """プロジェクト名 → id。所属はタグ(名前)で持つので、外に出すときに引き直す。"""
    return {p.name: p.id for p in tasks.list_projects()}


def _project_name(project_id: int) -> str:
    return tasks.require_project(project_id).name


def _task_json(task: tasks.Task, project_ids: dict[str, int]) -> dict:
    body = {
        "id": task.doc_id,
        "title": task.title,
        # 画面が編集するのは**タイトル行を除いた残り**。メモ本体はタイトルを先頭に含むので、
        # そのまま返すと編集画面にタイトルが二重に出る(直しても保存で組み立て直される)
        "body": tasks.body_rest(task),
        "status": task.status,
        "flagged": task.flagged,
        "sortOrder": task.sort_order,
        "createdAt": task.created_at,
        "updatedAt": task.updated_at,
    }
    # 未分類なら projectId ごと落とす(画面の型が「欠落する」前提)
    if task.project and task.project in project_ids:
        body["projectId"] = project_ids[task.project]
    return body


def _project_json(project: tasks.Project) -> dict:
    return {
        "id": project.id,
        "name": project.name,
        "repoUrls": project.repo_urls,
        "description": project.description or None,
        "archived": project.archived,
        "sortOrder": project.sort_order,
        "createdAt": project.created_at,
        "updatedAt": project.updated_at,
    }


def _rule_json(rule: tasks.Rule) -> dict:
    return {
        "id": rule.doc_id,
        "title": rule.title,
        "body": rule.body,
        "enabled": rule.enabled,
        "sortOrder": rule.sort_order,
        "createdAt": rule.created_at,
        "updatedAt": rule.updated_at,
    }


# ---- プロジェクト -----------------------------------------------------------


class ProjectInput(_Input):
    name: str | None = None
    repo_urls: list[str] | None = None
    description: str | None = None
    archived: bool | None = None


class OrderInput(_Input):
    ids: list[int]


@router.get("/projects")
def list_projects(archived: str | None = Query(None)) -> list[dict]:
    return [_project_json(p) for p in tasks.list_projects(_optional_bool(archived))]


@router.post("/projects", status_code=201)
def create_project(body: ProjectInput) -> dict:
    project = tasks.create_project(body.name or "", body.description, body.repo_urls)
    return _project_json(project)


@router.put("/projects/order")
def reorder_projects(body: OrderInput) -> list[dict]:
    return [_project_json(p) for p in tasks.reorder_projects(body.ids)]


@router.patch("/projects/{project_id}")
def update_project(project_id: int, body: ProjectInput) -> dict:
    project = tasks.update_project(
        project_id,
        name=body.name,
        description=body.description,
        repo_urls=body.repo_urls,
        archived=body.archived,
    )
    return _project_json(project)


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: int) -> None:
    tasks.delete_project(project_id)


# ---- タスク -----------------------------------------------------------------


class TaskInput(_Input):
    project_id: int | None = None
    title: str | None = None
    body: str | None = None
    status: str | None = None
    flagged: bool | None = None


class TaskOrderInput(_Input):
    project_id: int | None = None
    ids: list[int]


@router.get("/tasks")
def list_tasks(
    projectId: int | None = Query(None),  # 画面が送るクエリ名に合わせる
    status: str | None = Query(None),
    done: str | None = Query(None),
    page: int = Query(0, ge=0),
    size: int = Query(20, ge=1),
):
    want_done = _optional_bool(done)
    project = _project_name(projectId) if projectId is not None else None
    ids = _project_ids()
    if want_done is True:
        paged = tasks.list_done_tasks(project, page, size)
        total, size = paged["total"], paged["size"]
        return {
            "items": [_task_json(t, ids) for t in paged["items"]],
            "total": total,
            "page": paged["page"],
            "size": size,
            "totalPages": (total + size - 1) // size,
        }
    if want_done is False:
        return [_task_json(t, ids) for t in tasks.list_active_tasks(project)]
    return [_task_json(t, ids) for t in tasks.list_tasks(project, status)]


@router.post("/tasks", status_code=201)
def create_task(body: TaskInput) -> dict:
    project = _project_name(body.project_id) if body.project_id else None
    task = tasks.create_task(body.title or "", body.body, project, body.status)
    return _task_json(task, _project_ids())


@router.put("/tasks/order")
def reorder_tasks(body: TaskOrderInput) -> list[dict]:
    project = _project_name(body.project_id) if body.project_id else None
    reordered = tasks.reorder_tasks(project, body.ids)
    return [_task_json(t, _project_ids()) for t in reordered]


@router.get("/tasks/export")
def export_tasks() -> dict:
    return tasks.export_tasks()


@router.post("/tasks/import")
def import_tasks(dryRun: bool = Query(False), body: dict = Body(...)) -> dict:  # 同上
    return tasks.import_tasks(body, dry_run=dryRun)


@router.get("/tasks/{task_id}")
def get_task(task_id: int) -> dict:
    task = tasks.require_task(task_id)
    body = _task_json(task, _project_ids())
    if task.project:
        body["projectName"] = task.project
    return body


@router.patch("/tasks/{task_id}")
def update_task(task_id: int, body: TaskInput) -> dict:
    # projectId は「変更しない(未指定)」と「紐づけを外す」を分ける必要がある。
    # doc_id は 1 から振られるので、0 を「外す」の意味に使ってもぶつからない。
    unlink = body.project_id == tasks.UNLINK_PROJECT_ID
    project = _project_name(body.project_id) if (body.project_id and not unlink) else None
    task = tasks.update_task(
        task_id,
        title=body.title,
        body=body.body,
        project=project,
        status=body.status,
        flagged=body.flagged,
        unlink_project=unlink,
    )
    return _task_json(task, _project_ids())


@router.delete("/tasks/{task_id}", status_code=204)
def delete_task(task_id: int) -> None:
    tasks.delete_task(task_id)


# ---- ルール -----------------------------------------------------------------


class RuleInput(_Input):
    title: str | None = None
    body: str | None = None
    enabled: bool | None = None


class RuleImportInput(_Input):
    markdown: str
    replace: bool = False
    dry_run: bool = False


class RuleSettingsInput(_Input):
    rules_repo_url: str | None = None


@router.get("/rules")
def list_rules() -> list[dict]:
    return [_rule_json(r) for r in tasks.list_rules()]


@router.get("/rules/combined")
def combined_rules() -> dict:
    return {"markdown": tasks.combined()}


@router.get("/rules/settings")
def rule_settings() -> dict:
    return {"rulesRepoUrl": tasks.rules_repo_url()}


@router.patch("/rules/settings")
def update_rule_settings(body: RuleSettingsInput) -> dict:
    # null は「変更しない」、空文字は「消す」(PATCH の流儀どおり)
    if body.rules_repo_url is not None:
        tasks.set_rules_repo_url(body.rules_repo_url)
    return {"rulesRepoUrl": tasks.rules_repo_url()}


@router.post("/rules/import")
def import_rules(body: RuleImportInput) -> dict:
    result = tasks.import_rules(body.markdown, replace=body.replace, dry_run=body.dry_run)
    return {"titles": result["titles"], "rules": [_rule_json(r) for r in result["rules"]]}


@router.put("/rules/order")
def reorder_rules(body: OrderInput) -> list[dict]:
    return [_rule_json(r) for r in tasks.reorder_rules(body.ids)]


@router.post("/rules", status_code=201)
def create_rule(body: RuleInput) -> dict:
    enabled = True if body.enabled is None else body.enabled
    return _rule_json(tasks.create_rule(body.title or "", body.body or "", enabled))


@router.patch("/rules/{rule_id}")
def update_rule(rule_id: int, body: RuleInput) -> dict:
    return _rule_json(tasks.update_rule(rule_id, body.title, body.body, body.enabled))


@router.delete("/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: int) -> None:
    tasks.delete_rule(rule_id)


# ---- そのほかのメモ ----------------------------------------------------------
#
# タスク・プロジェクト・ルールのどれでもないメモ。短期記憶に溜まる大半はこちらで、
# 画面から見る手段が無かった(決定・環境・runbook・トラブルシュート…)。
# **読み取りだけ**にしてある —— 書くのは MCP の remember か本体の REST で、
# ここに増やすと同じことをする口が 3 つになる。


def _note_json(note: tasks.Note) -> dict:
    return {
        "id": note.doc_id,
        "title": note.title,
        "body": note.body,
        "tags": note.tags,
        "createdAt": note.created_at,
        "updatedAt": note.updated_at,
        # 本体のブラウズ画面へ渡す入口。全文と生の項目はあちらで見られる
        "url": f"/search/notes/doc/{note.doc_id}",
    }


@router.get("/notes")
def list_notes(
    tag: str | None = Query(None, description="タグで絞る。カンマ区切りで AND"),
    limit: int = Query(50, ge=1, le=tasks.NOTE_PAGE_SIZE_MAX),
    offset: int = Query(0, ge=0),
) -> dict:
    items, total = tasks.list_notes(tag=tag, limit=limit, offset=offset)
    return {"items": [_note_json(n) for n in items], "total": total}


@router.get("/notes/tags")
def list_note_tags() -> list[dict]:
    """絞り込みの候補。**そのほかのメモに付いているものだけ**を数える。"""
    return [{"tag": tag, "docs": docs} for tag, docs in tasks.note_tags()]


# ---- 生成物の見比べ ----------------------------------------------------------
#
# **読むのと印を付けるだけ。** 生成そのもの(`/v1/media/image` など)は課金が走るので、
# 外に出すこの面には載せない(`app/tasks_app.py` の冒頭)。ここにあるのは
# 「作られたものを並べて、どれがいいかを選ぶ」ところまで。


@router.get("/media/groups")
def media_groups(limit: int = Query(20, ge=1, le=100)) -> dict:
    """見比べる組。頼んだ AI が同じ `group` を付けた案がまとまって並ぶ。"""
    return {"groups": media.job_groups(limit)}


class MediaPickInput(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    note: str = ""


@router.post("/media/jobs/{job_id}/pick")
def media_pick(job_id: str, body: MediaPickInput) -> dict:
    """この案を採用と印す。**頼んだ AI はこれを見に来る。**"""
    return media.pick_job(job_id, body.note)


@router.delete("/media/jobs/{job_id}/pick")
def media_unpick(job_id: str) -> dict:
    return media.unpick_job(job_id)


@router.get("/media/picks")
def media_picks(
    limit: int = Query(20, ge=1, le=100), group: str = Query("")
) -> dict:
    return {"picks": media.picked_jobs(limit, group.strip())}
