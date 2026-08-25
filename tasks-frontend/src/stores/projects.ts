import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '@/api/client'
import type { Project, ProjectInput } from '@/api/types'

export const useProjectStore = defineStore('projects', () => {
  /** アーカイブ済みも含めた全件。表示側で絞る。 */
  const all = ref<Project[]>([])
  const loading = ref(false)
  const loaded = ref(false)

  const active = computed(() => all.value.filter((p) => !p.archived))
  const byId = computed(() => new Map(all.value.map((p) => [p.id, p])))

  /** 表示順は sortOrder 昇順(同値は名前順)。API の返却順と揃える。 */
  function sort(list: Project[]): Project[] {
    return [...list].sort((a, b) => a.sortOrder - b.sortOrder || a.name.localeCompare(b.name))
  }

  async function load(force = false) {
    if (loaded.value && !force) return
    loading.value = true
    try {
      all.value = await api.listProjects(undefined)
      loaded.value = true
    } finally {
      loading.value = false
    }
  }

  async function create(input: ProjectInput) {
    // 新規は並びの末尾に付く(sortOrder はサーバーが採番)
    const created = await api.createProject(input)
    all.value = sort([...all.value, created])
    return created
  }

  async function update(id: number, input: ProjectInput) {
    const updated = await api.updateProject(id, input)
    all.value = all.value.map((p) => (p.id === id ? updated : p))
    return updated
  }

  /** アーカイブ済みのプロジェクトを、紐づくタスクごと消す。 */
  async function remove(id: number) {
    await api.deleteProject(id)
    all.value = all.value.filter((p) => p.id !== id)
  }

  /** 並び替え。全プロジェクトの id を望む順で渡す。 */
  async function reorder(ids: number[]) {
    all.value = await api.reorderProjects(ids)
  }

  /**
   * 画面に出ている分だけの並びを、全体の並びに埋め戻して保存する。
   * トップは「タスクの残っているアーカイブ済み」だけを混ぜて出すため表示が全件と一致しない。
   * 出ていないプロジェクトは元の位置に据え置き、出ている分の枠だけを詰め替える。
   */
  async function reorderVisible(ordered: Project[]) {
    const visible = new Set(ordered.map((p) => p.id))
    const next = [...all.value]
    let i = 0
    for (let index = 0; index < next.length; index++) {
      if (visible.has(next[index].id)) next[index] = ordered[i++]
    }
    await reorder(next.map((p) => p.id))
  }

  function name(id: number): string {
    return byId.value.get(id)?.name ?? `#${id}`
  }

  return {
    all,
    active,
    byId,
    loading,
    loaded,
    load,
    create,
    update,
    remove,
    reorder,
    reorderVisible,
    name,
  }
})
