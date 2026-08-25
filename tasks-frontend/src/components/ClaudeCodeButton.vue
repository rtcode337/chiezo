<script setup lang="ts">
import { computed } from 'vue'
import type { Task } from '@/api/types'
import { claudeCodeUrl } from '@/lib/claudeCode'
import { useRuleStore } from '@/stores/rules'

const props = defineProps<{ task: Task; repoUrls?: string[] }>()

// 規約リポジトリを repositories に足すため設定だけ読む(ストア側で同時多発は 1 回にまとまる)。
// 取れなくても ✳ 自体は使えるので失敗は握りつぶす(未ロードのまま次の表示で再試行される)
const rules = useRuleStore()
rules.loadSettings().catch(() => {})

/**
 * iOS のホーム画面追加(スタンドアロン=PWA)で動いているか。
 * navigator.standalone は iOS Safari 独自プロパティなので iOS 判定を兼ねる。
 */
const isIosStandalone =
  typeof navigator !== 'undefined' &&
  (navigator as unknown as { standalone?: boolean }).standalone === true

const href = computed(() => {
  const url = claudeCodeUrl(props.task, props.repoUrls, rules.rulesRepoUrl)
  // iOS の PWA では外部リンクがアプリ内ブラウザで開いてしまう(claude.ai の
  // ログインセッションを共有しない)。非公式だが iOS 17+ の x-safari- スキームで
  // 既定ブラウザの Safari 本体に渡す。効かなくなったら素の URL に戻すこと
  return isIosStandalone ? `x-safari-${url}` : url
})
</script>

<template>
  <!-- タスク内容をプリフィルして Claude Code を開くハンドオフボタン。
       スマホではユニバーサルリンクで Claude アプリが開きプリフィルは失われる
       (空タブ経由・中継ページ /handoff 経由の JS 遷移でも回避できなかった)。
       Claude アプリ側でリンクを一度「Safari で開く」にすればブラウザ版が開く -->
  <a
    class="icon"
    :href="href"
    target="_blank"
    rel="noopener noreferrer"
    aria-label="Claude Code で開く"
    title="Claude Code で開く(内容をプリフィル)"
    @click.stop
  >
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
      <path
        d="M12 3.5v17M3.5 12h17M6 6l12 12M18 6L6 18"
        fill="none"
        stroke="currentColor"
        stroke-width="1.7"
        stroke-linecap="round"
      />
    </svg>
  </a>
</template>

<style scoped>
.icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.875rem;
  height: 1.875rem;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  color: var(--muted);
  cursor: pointer;
  flex-shrink: 0;
}

.icon:hover {
  color: var(--accent);
  border-color: var(--accent);
}
</style>
