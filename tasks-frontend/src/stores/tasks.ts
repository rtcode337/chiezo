import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '@/api/client'
import type { Task, TaskDetail, TaskExport, TaskImportResult, TaskInput } from '@/api/types'

/**
 * 同じプロジェクト内での並び。手動並び順(昇順)が先で、同値は作成日時降順。
 * 並び替えは 1, 2, 3, … を振るので、未並び替えの 0(= 新しく放り込んだタスク)が先頭に来る。
 */
export function compareInProject(a: Task, b: Task): number {
  if (a.sortOrder !== b.sortOrder) return a.sortOrder - b.sortOrder
  if (a.createdAt !== b.createdAt) return a.createdAt < b.createdAt ? 1 : -1
  return b.id - a.id
}

/**
 * 未完了(done 以外)のタスクをここに保持し、画面側でフィルタする。
 * 完了タスクは件数が増えるためストアには持たず、一覧画面がページングで直接取得する。
 *
 * ここでの並びは作成日時降順。手動並び順(sortOrder)は *プロジェクト内* の順序なので、
 * プロジェクトをまたぐこのフラットな配列には効かせない
 * (トップのグループ内だけ {@link compareInProject} で並べ直す)。
 */
export const useTaskStore = defineStore('tasks', () => {
  const active = ref<Task[]>([])
  const loading = ref(false)
  const loaded = ref(false)

  function sort(list: Task[]): Task[] {
    return [...list].sort((a, b) => {
      if (a.createdAt !== b.createdAt) return a.createdAt < b.createdAt ? 1 : -1
      return b.id - a.id
    })
  }

  /** 更新結果を active に反映(done になったら active から外す)。 */
  function apply(updated: Task) {
    if (updated.status === 'done') {
      active.value = active.value.filter((t) => t.id !== updated.id)
      return
    }
    const exists = active.value.some((t) => t.id === updated.id)
    active.value = sort(
      exists ? active.value.map((t) => (t.id === updated.id ? updated : t)) : [updated, ...active.value],
    )
  }

  async function load(force = false) {
    if (loaded.value && !force) return
    loading.value = true
    try {
      active.value = sort(await api.listTasks({ done: false }))
      loaded.value = true
    } finally {
      loading.value = false
    }
  }

  async function create(input: TaskInput) {
    const created = await api.createTask(input)
    if (created.status !== 'done') active.value = sort([created, ...active.value])
    return created
  }

  async function update(id: number, input: TaskInput) {
    const updated = await api.updateTask(id, input)
    apply(updated)
    return updated
  }

  /** 完了にする(削除ではなく status=done)。 */
  async function complete(id: number) {
    return update(id, { status: 'done' })
  }

  /**
   * プロジェクト内の並び替え。ids は望む順、未紐づけのかたまりは projectId=null。
   * サーバーが振り直した sortOrder で手元を差し替える。
   */
  async function reorder(projectId: number | null, ids: number[]) {
    const updated = new Map((await api.reorderTasks(projectId, ids)).map((t) => [t.id, t]))
    active.value = sort(active.value.map((t) => updated.get(t.id) ?? t))
  }

  async function remove(id: number) {
    await api.deleteTask(id)
    active.value = active.value.filter((t) => t.id !== id)
  }

  /**
   * プロジェクトごと消えたタスクを手元から落とす。
   * 削除自体はサーバー側でまとめてやっているので、ここは表示を合わせるだけ。
   */
  function dropByProject(projectId: number) {
    active.value = active.value.filter((t) => t.projectId !== projectId)
  }

  function detail(id: number): Promise<TaskDetail> {
    return api.getTask(id)
  }

  /** 未完了タスクをプロジェクト名・リポジトリ付きで書き出す。 */
  function exportAll(): Promise<TaskExport> {
    return api.exportTasks()
  }

  /**
   * 書き出したものを読み込む。dryRun なら書き込まず予定だけ返す。
   * 本実行ではプロジェクトが増えることもあるので、呼び出し側で
   * プロジェクト一覧も取り直す(ここではタスクだけ面倒を見る)。
   */
  async function importAll(data: TaskExport, dryRun = false): Promise<TaskImportResult> {
    const result = await api.importTasks(data, dryRun)
    if (!dryRun) await load(true)
    return result
  }

  return {
    active,
    loading,
    loaded,
    load,
    create,
    update,
    complete,
    reorder,
    remove,
    dropByProject,
    detail,
    exportAll,
    importAll,
  }
})
