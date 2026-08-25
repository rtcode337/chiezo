import { compareInProject } from '@/stores/tasks'
import type { Project, Task } from '@/api/types'

/** 未分類(プロジェクト未設定)のかたまりを指す key。並び替えの送信先は projectId=null */
export const UNLINKED_KEY = 'none'

/** 一覧の「プロジェクトごとのかたまり」。トップ・アーカイブ・完了の各一覧で共有する */
export interface TaskGroup {
  /** 並び替えの送信先を兼ねる識別子。`p{projectId}` か {@link UNLINKED_KEY} */
  key: string
  /** null = 未分類 */
  project: Project | null
  tasks: Task[]
}

/** 未分類ではない = プロジェクトに紐づくグループ。見出しの並び替え・編集の対象はこちらだけ */
export function isProjectGroup(group: TaskGroup): group is TaskGroup & { project: Project } {
  return group.project !== null
}

/** グループの key からタスク並び替えの送信先を得る。未分類は null */
export function groupProjectId(key: string): number | null {
  return key === UNLINKED_KEY ? null : Number(key.slice(1))
}

/**
 * タスクを、渡した `projects` の順にグループ化する。
 * **タスクが 0 件のプロジェクトも残す** —— そこへ放り込む導線になるため。
 * 出したいプロジェクトだけを呼び出し側で絞って渡す(トップは非アーカイブ、アーカイブ一覧はその逆)。
 */
export function buildTaskGroups(todo: Task[], projects: Project[]): TaskGroup[] {
  const byProject = new Map<number, Task[]>()
  for (const task of todo) {
    if (task.projectId == null) continue
    const list = byProject.get(task.projectId)
    if (list) list.push(task)
    else byProject.set(task.projectId, [task])
  }
  return projects.map((project) => ({
    key: `p${project.id}`,
    project,
    tasks: (byProject.get(project.id) ?? []).sort(compareInProject),
  }))
}

/**
 * どのプロジェクトにも紐づいていないタスクを「未分類」のかたまりにして、一覧の**末尾**に足す。
 * `keepEmpty` を立てると該当 0 件でも足す —— トップでは放り込み先・ドラッグで紐づけを外す先に
 * なるため常に出す。完了一覧のように「置き場」の意味が無い画面では立てず、0 件なら足さない。
 */
export function withUnlinkedGroup(
  groups: TaskGroup[],
  todo: Task[],
  keepEmpty = false,
): TaskGroup[] {
  const tasks = todo.filter((task) => task.projectId == null).sort(compareInProject)
  if (tasks.length === 0 && !keepEmpty) return groups
  return [...groups, { key: UNLINKED_KEY, project: null, tasks }]
}
