import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '@/api/client'
import type { Rule, RuleInput } from '@/api/types'

/**
 * すべての Claude Code 環境に効かせたい共通ルール。
 * 並び順がそのまま連結順になるので、表示順は sortOrder 昇順で固定する。
 */
export const useRuleStore = defineStore('rules', () => {
  const all = ref<Rule[]>([])
  const loading = ref(false)
  const loaded = ref(false)

  const enabledCount = computed(() => all.value.filter((r) => r.enabled).length)

  // 規約リポジトリ(連結ルールを CLAUDE.md として置く先)。未設定なら null。
  // ✳ ハンドオフの repositories に常に付与するため、ルール一覧とは別に軽く読める
  const rulesRepoUrl = ref<string | null>(null)
  const settingsLoaded = ref(false)
  let settingsLoading: Promise<void> | null = null

  /** 規約リポジトリ設定を読む。✳ を出すカードごとに呼ばれるので同時多発は 1 回にまとめる */
  function loadSettings(force = false): Promise<void> {
    if (settingsLoaded.value && !force) return Promise.resolve()
    if (settingsLoading && !force) return settingsLoading
    settingsLoading = (async () => {
      try {
        rulesRepoUrl.value = (await api.ruleSettings()).rulesRepoUrl ?? null
        settingsLoaded.value = true
      } finally {
        settingsLoading = null
      }
    })()
    return settingsLoading
  }

  /** 規約リポジトリの更新。空文字で解除(サーバー側の PATCH 規約と同じ)。 */
  async function updateRulesRepoUrl(value: string) {
    rulesRepoUrl.value = (await api.updateRuleSettings({ rulesRepoUrl: value })).rulesRepoUrl ?? null
    settingsLoaded.value = true
  }

  function sort(list: Rule[]): Rule[] {
    return [...list].sort((a, b) => a.sortOrder - b.sortOrder || a.id - b.id)
  }

  async function load(force = false) {
    if (loaded.value && !force) return
    loading.value = true
    try {
      all.value = sort(await api.listRules())
      loaded.value = true
    } finally {
      loading.value = false
    }
  }

  async function create(input: RuleInput) {
    // 新規は並びの末尾に付く(sortOrder はサーバーが採番)
    const created = await api.createRule(input)
    all.value = sort([...all.value, created])
    return created
  }

  async function update(id: number, input: RuleInput) {
    const updated = await api.updateRule(id, input)
    all.value = sort(all.value.map((r) => (r.id === id ? updated : r)))
    return updated
  }

  async function remove(id: number) {
    await api.deleteRule(id)
    all.value = all.value.filter((r) => r.id !== id)
  }

  /** 並び替え。全ルールの id を望む順で渡す。 */
  async function reorder(ids: number[]) {
    all.value = sort(await api.reorderRules(ids))
  }

  /** 有効なルールを連結した Markdown をサーバーから取り直す。 */
  async function combined(): Promise<string> {
    return (await api.combinedRules()).markdown
  }

  /**
   * まとめたルールの Markdown を貼り付けて一覧へ戻す(combined の逆)。
   * dryRun なら書き込まず、取り込む見出しだけ返す(取り込み前の確認用)。
   */
  async function importMarkdown(
    markdown: string,
    options: { replace?: boolean; dryRun?: boolean } = {},
  ) {
    const result = await api.importRules({ markdown, ...options })
    // 入れ替えでは既存の id が消えるので、返ってきた全件でそのまま置き換える
    if (!options.dryRun) {
      all.value = sort(result.rules)
      loaded.value = true
    }
    return result
  }

  return {
    all,
    enabledCount,
    loading,
    loaded,
    load,
    create,
    update,
    remove,
    reorder,
    combined,
    importMarkdown,
    rulesRepoUrl,
    loadSettings,
    updateRulesRepoUrl,
  }
})
