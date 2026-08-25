<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRuleStore } from '@/stores/rules'
import { usePullToRefresh } from '@/lib/pullToRefresh'
import { useDragSort } from '@/lib/dragSort'
import { repoSlug } from '@/lib/claudeCode'
import { backdropClose } from '@/lib/backdropClose'
import { stampedName } from '@/lib/fileTransfer'
import type { Rule } from '@/api/types'
import ErrorBanner from '@/components/ErrorBanner.vue'
import CopyButton from '@/components/CopyButton.vue'
import FileSaveButton from '@/components/FileSaveButton.vue'
import RuleFormModal from '@/components/RuleFormModal.vue'
import RuleImportModal from '@/components/RuleImportModal.vue'

/**
 * すべての Claude Code 環境に効かせたい共通ルール。
 * 「まとめて表示」で有効なルールを 1 本の Markdown に連結し、
 * それを各環境の指示ファイル(`~/.claude/rules/` など)へ貼って使う。
 */
const rules = useRuleStore()

usePullToRefresh(async () => {
  await Promise.all([rules.load(true), rules.loadSettings(true)])
  syncRepoInput()
})

const error = ref<string | null>(null)

/** null = 新規作成 */
const editing = ref<Rule | null>(null)
const modalOpen = ref(false)

// 貼り付け取り込み(「まとめて表示」の逆)
const importOpen = ref(false)

// 連結結果のモーダル
const combinedOpen = ref(false)
const combinedBackdrop = backdropClose(() => (combinedOpen.value = false))
const combined = ref('')
const combining = ref(false)

onMounted(async () => {
  try {
    await Promise.all([rules.load(true), rules.loadSettings(true)])
    syncRepoInput()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
})

// 規約リポジトリ(連結ルールを CLAUDE.md として置く先)。✳ のハンドオフに常に含まれる
const repoInput = ref('')
const savingRepo = ref(false)
const repoDirty = computed(() => repoInput.value.trim() !== (rules.rulesRepoUrl ?? ''))

function syncRepoInput() {
  repoInput.value = rules.rulesRepoUrl ?? ''
}

async function saveRepo() {
  const value = repoInput.value.trim()
  // 入力があるのに owner/repo に解釈できないなら保存前に弾く(空は「解除」なので通す)
  if (value !== '' && repoSlug(value) === null) {
    error.value = '規約リポジトリは GitHub の URL か owner/repo の形で指定してください'
    return
  }
  savingRepo.value = true
  error.value = null
  try {
    await rules.updateRulesRepoUrl(value)
    syncRepoInput()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    savingRepo.value = false
  }
}

function openCreate() {
  editing.value = null
  modalOpen.value = true
}

function openEdit(rule: Rule) {
  editing.value = rule
  modalOpen.value = true
}

/** 一覧のトグル。有効なものだけが「まとめて表示」に含まれる。 */
async function toggleEnabled(rule: Rule) {
  error.value = null
  try {
    await rules.update(rule.id, { enabled: !rule.enabled })
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

async function showCombined() {
  combining.value = true
  error.value = null
  try {
    // 連結はサーバー側で行う。貼り付ける本文と API が返す本文を一致させるため
    combined.value = await rules.combined()
    combinedOpen.value = true
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    combining.value = false
  }
}

// 行を長押ししてドラッグで並び替え。並び順がそのまま連結順になる
const sorter = useDragSort<Rule>(async (_key, ordered) => {
  error.value = null
  try {
    await rules.reorder(ordered.map((r) => r.id))
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
})
</script>

<template>
  <section>
    <ErrorBanner v-if="error" :message="error" />

    <div class="head">
      <h1 class="title">ルール</h1>
      <div class="head__actions">
        <!-- まとめた Markdown からの復元。貼り先に残っている 1 本がバックアップになる -->
        <button type="button" class="head__combine" @click="importOpen = true">取り込み</button>
        <button
          type="button"
          class="head__combine"
          :disabled="combining || rules.all.length === 0"
          @click="showCombined"
        >
          {{ combining ? '集約中…' : `まとめて表示 (${rules.enabledCount})` }}
        </button>
        <button type="button" class="button head__add" @click="openCreate">＋ 新規</button>
      </div>
    </div>

    <p class="lead">
      すべての Claude Code 環境に効かせたい共通ルール。「まとめて表示」で 1 本の Markdown
      に連結できるので、それを各環境の指示ファイルに貼って使う。
    </p>

    <p v-if="rules.loading" class="muted">読み込み中…</p>
    <p v-else-if="rules.all.length === 0" class="muted">まだルールはありません。</p>

    <ul v-else class="list">
      <li
        v-for="(rule, i) in sorter.view('rules', rules.all)"
        :key="rule.id"
        class="item"
        :class="{ 'item--dragging': sorter.isDragging('rules', i) }"
        @pointerdown="sorter.start('rules', rules.all, i, '.list', $event)"
        @click.capture="sorter.clickGuard"
      >
        <div class="row">
          <button type="button" class="row__open" @click="openEdit(rule)">
            <span class="row__title" :class="{ 'row__title--off': !rule.enabled }">
              {{ rule.title }}
            </span>
            <span class="row__body">{{ rule.body }}</span>
          </button>
          <!-- data-no-drag: トグルの長押しはドラッグにしない -->
          <label class="toggle" data-no-drag :title="rule.enabled ? '有効' : '無効'">
            <input
              type="checkbox"
              :checked="rule.enabled"
              :aria-label="`${rule.title} を有効にする`"
              @change="toggleEnabled(rule)"
            />
            <span class="toggle__track"><span class="toggle__knob" /></span>
          </label>
        </div>
      </li>
    </ul>

    <!-- 規約リポジトリ。設定すると ✳ で開くセッションに常に含まれ、
         そのルート直下の CLAUDE.md(= まとめたルールの置き場)が読み込まれる -->
    <div class="repo">
      <label class="repo__label" for="rules-repo">規約リポジトリ</label>
      <div class="repo__row">
        <input
          id="rules-repo"
          v-model="repoInput"
          class="repo__input"
          type="text"
          placeholder="owner/repo か GitHub の URL"
          autocomplete="off"
        />
        <button
          type="button"
          class="button repo__save"
          :disabled="savingRepo || !repoDirty"
          @click="saveRepo"
        >
          {{ savingRepo ? '保存中…' : '保存' }}
        </button>
      </div>
      <p class="repo__hint">
        まとめたルールを CLAUDE.md として置いている GitHub リポジトリ。設定すると
        ✳(Claude Code へのハンドオフ)で開くセッションに常に含まれ、共通ルールが効く。
        空にして保存すると解除。
      </p>
    </div>

    <RuleFormModal v-if="modalOpen" :rule="editing" @close="modalOpen = false" />

    <RuleImportModal v-if="importOpen" @close="importOpen = false" />

    <!-- 連結結果。貼り付けるための素の Markdown をそのまま出す -->
    <!-- 背景クリックで閉じる。本文を選択して背景で指を離したときは閉じない -->
    <div v-if="combinedOpen" class="modal" v-on="combinedBackdrop">
      <div class="modal__panel">
        <div class="modal__head">
          <h2 class="modal__title">まとめたルール</h2>
          <div class="modal__tools">
            <FileSaveButton
              v-if="combined"
              :text="combined"
              :filename="stampedName('chiezo-rules', 'md')"
              mime="text/markdown"
            />
            <CopyButton icon :text="combined" label="ルール全文をコピー" />
          </div>
        </div>
        <p v-if="combined === ''" class="muted">有効なルールがありません。</p>
        <template v-else>
          <p class="modal__hint">
            コピーして、規約リポジトリの CLAUDE.md に丸ごと貼り替える(✳ で開くセッションに効く)か、
            CLI 版なら <code>~/.claude/rules/</code> に .md を置いて貼る(そのマシンの全リポジトリに効く)。
          </p>
          <pre class="combined">{{ combined }}</pre>
        </template>
        <div class="actions">
          <button type="button" class="button button--ghost" @click="combinedOpen = false">
            閉じる
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.75rem;
  /* 狭い画面ではボタン群を下段に折り返す */
  flex-wrap: wrap;
  padding: 1rem 0 0.5rem;
}

.head__actions {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.head__add {
  width: auto;
  padding: 0.375rem 0.875rem;
  font-size: 0.8125rem;
  flex-shrink: 0;
}

.head__combine {
  padding: 0.375rem 0.75rem;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  color: var(--muted);
  font-size: 0.8125rem;
  font-family: inherit;
  cursor: pointer;
  white-space: nowrap;
}

.head__combine:hover:not(:disabled) {
  color: var(--accent);
  border-color: var(--accent);
}

.head__combine:disabled {
  opacity: 0.4;
  cursor: default;
}

.title {
  margin: 0;
  font-size: 1.125rem;
}

.lead {
  /* 広い画面でも 1 行が長くなりすぎないところで折り返す(読み物なので伸ばさない) */
  max-width: var(--reading-max);
  margin: 0 0 1rem;
  font-size: 0.75rem;
  line-height: 1.6;
  color: var(--muted-dim);
}

.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

/* 掴んでいる行。持ち上がって見えるようにする */
.item--dragging .row {
  opacity: 0.9;
  border-color: var(--accent);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.18);
}

.row {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.75rem;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
  /* 長押しでドラッグを始めるので、iOS の長押しメニューと文字選択は出さない */
  -webkit-touch-callout: none;
  user-select: none;
}

.row:hover {
  border-color: var(--accent);
}

/* 行の本体。押すと編集モーダル */
.row__open {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 0.25rem;
  padding: 0;
  border: none;
  background: none;
  color: var(--text);
  text-align: left;
  font: inherit;
  cursor: pointer;
}

.row__title {
  font-weight: 600;
}

.row__title--off {
  color: var(--muted-dim);
}

.row__body {
  max-width: 100%;
  font-size: 0.75rem;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 有効/無効のトグル。チェックボックス本体は隠してトラックとノブで見せる */
.toggle {
  position: relative;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  cursor: pointer;
  /* トグルの上では長押しメニューを許す(ドラッグ対象から外しているため) */
  -webkit-touch-callout: default;
}

.toggle input {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  margin: 0;
  opacity: 0;
  cursor: pointer;
}

.toggle__track {
  display: flex;
  align-items: center;
  width: 2.25rem;
  height: 1.25rem;
  padding: 0.125rem;
  border-radius: 999px;
  background: var(--border);
  transition: background 0.15s ease;
}

.toggle__knob {
  width: 1rem;
  height: 1rem;
  border-radius: 50%;
  background: var(--surface);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.25);
  transition: transform 0.15s ease;
}

.toggle input:checked + .toggle__track {
  background: var(--accent);
}

.toggle input:checked + .toggle__track .toggle__knob {
  transform: translateX(1rem);
}

.toggle input:focus-visible + .toggle__track {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

/* 規約リポジトリの設定。一覧の下に区切って置く */
.repo {
  margin-top: 1.5rem;
  padding-top: 1rem;
  border-top: 1px solid var(--border);
}

.repo__label {
  display: block;
  margin-bottom: 0.375rem;
  font-size: 0.8125rem;
  font-weight: 600;
  color: var(--muted);
}

.repo__row {
  display: flex;
  gap: 0.5rem;
}

.repo__input {
  flex: 1;
  min-width: 0;
  padding: 0.5rem 0.625rem;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  color: var(--text);
  font: inherit;
  font-size: 0.8125rem;
}

.repo__input:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.repo__save {
  width: auto;
  padding: 0.375rem 0.875rem;
  font-size: 0.8125rem;
  flex-shrink: 0;
}

.repo__hint {
  margin: 0.5rem 0 0;
  font-size: 0.6875rem;
  line-height: 1.6;
  color: var(--muted-dim);
}

.modal {
  position: fixed;
  inset: 0;
  background: var(--overlay);
  display: flex;
  align-items: flex-end;
  justify-content: center;
  padding: 1rem;
  z-index: 20;
}

.modal__panel {
  width: 100%;
  max-width: 40rem;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  padding: 1.25rem;
  border: 1px solid var(--border);
  border-radius: 14px;
  background: var(--surface-raised);
  margin-bottom: env(safe-area-inset-bottom);
}

.modal__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
}

.modal__title {
  margin: 0;
  font-size: 1rem;
}

/* 右上のアイコン(ファイルに書き出す → コピーの順)。見た目は main.css の .icon-button */
.modal__tools {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

/* 貼り先の説明。コピーした Markdown をどこへ置くか迷わないように */
.modal__hint {
  margin: 0;
  font-size: 0.6875rem;
  line-height: 1.6;
  color: var(--muted-dim);
}

.modal__hint code {
  font-size: 0.6875rem;
}

/* 貼り付ける素の Markdown。整形せずそのまま出す */
.combined {
  margin: 0;
  max-height: 60vh;
  overflow: auto;
  padding: 0.75rem;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
  font-size: 0.75rem;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.actions {
  display: flex;
}

.actions .button {
  flex: 1;
}

.muted {
  color: var(--muted);
}

@media (min-width: 40rem) {
  .modal {
    align-items: center;
  }
}
</style>
