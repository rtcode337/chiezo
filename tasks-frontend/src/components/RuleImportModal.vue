<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRuleStore } from '@/stores/rules'
import { backdropClose } from '@/lib/backdropClose'
import ErrorBanner from '@/components/ErrorBanner.vue'
import FileLoadButton from '@/components/FileLoadButton.vue'

/**
 * まとめたルールの Markdown を貼り付けて一覧へ戻す(「まとめて表示」の逆)。
 * 貼り先に残っている 1 本の Markdown が、そのままバックアップとして使える。
 *
 * 区切りの解釈はサーバー側(`RuleMarkdownParser`)に任せる —— 連結をサーバーで
 * 行っているのと同じ理由で、画面とサーバーで解釈がずれると往復しなくなるため。
 * 取り込む前に dryRun で見出しを出すのは、入れ替えが取り消せないから。
 */
const emit = defineEmits<{ close: [] }>()

const backdrop = backdropClose(() => emit('close'))

const rules = useRuleStore()
const error = ref<string | null>(null)
const busy = ref(false)

const markdown = ref('')
/** 既存をどうするか。追加なら末尾に足し、入れ替えなら全消ししてから入れる */
const replace = ref(false)
/** dryRun で得た見出し。null = まだ確認していない */
const preview = ref<string[] | null>(null)

const canSubmit = computed(() => markdown.value.trim() !== '' && !busy.value)

/** 本文か取り込み方を変えたら、確認済みの見出しは古くなるので破棄する */
function invalidatePreview() {
  preview.value = null
}

/** 選ばれたファイルの中身を入力欄へ流し込む(取り込みは走らせない。貼ったときと同じ流れ) */
function loadFile(text: string) {
  markdown.value = text
  error.value = null
  invalidatePreview()
}

async function confirm() {
  if (!canSubmit.value) return
  busy.value = true
  error.value = null
  try {
    preview.value = (await rules.importMarkdown(markdown.value, { dryRun: true })).titles
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function run() {
  if (!canSubmit.value || preview.value === null) return
  if (replace.value && !window.confirm(`既存のルールをすべて消して ${preview.value.length} 件に入れ替えます。よろしいですか?`)) {
    return
  }
  busy.value = true
  error.value = null
  try {
    await rules.importMarkdown(markdown.value, { replace: replace.value })
    emit('close')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <!-- 背景クリックで閉じる。貼り付けた本文を選択して背景で指を離したときは閉じない -->
  <div class="modal" v-on="backdrop">
    <div class="modal__panel">
      <div class="modal__head">
        <h2 class="modal__title">ルールを取り込む</h2>
        <FileLoadButton
          accept=".md,.markdown,text/markdown,text/plain"
          @load="loadFile"
          @error="error = $event"
        />
      </div>

      <ErrorBanner v-if="error" :message="error" />

      <label class="field">
        <span class="field__label">まとめたルール<span class="field__required">必須</span></span>
        <span class="field__hint">
          規約リポジトリの CLAUDE.md や <code>~/.claude/rules/</code> に置いた .md の中身を丸ごと貼る。
          <code>## 見出し</code> ごとに 1 本のルールへ戻す(前置きと「規約リポジトリの扱い」は
          連結時に自動で付くので取り込まない)。
        </span>
        <textarea
          v-model="markdown"
          rows="10"
          required
          placeholder="# 共通ルール&#10;&#10;## 日本語で書く&#10;&#10;…"
          @input="invalidatePreview"
        />
      </label>

      <fieldset class="modes">
        <legend class="field__label">既存のルール</legend>
        <label class="mode">
          <input type="radio" :value="false" v-model="replace" @change="invalidatePreview" />
          <span>残して末尾に追加</span>
        </label>
        <label class="mode">
          <input type="radio" :value="true" v-model="replace" @change="invalidatePreview" />
          <span>すべて消して入れ替え</span>
        </label>
      </fieldset>

      <!-- 何が入るかを先に見せる。入れ替えは取り消せないため -->
      <div v-if="preview" class="preview">
        <p v-if="preview.length === 0" class="muted">取り込めるルールがありません。</p>
        <template v-else>
          <p class="preview__head">{{ preview.length }} 件を取り込みます</p>
          <ol class="preview__list">
            <li v-for="title in preview" :key="title">{{ title }}</li>
          </ol>
        </template>
      </div>

      <div class="actions">
        <button type="button" class="button button--ghost" @click="emit('close')">
          キャンセル
        </button>
        <button v-if="preview === null" type="button" class="button" :disabled="!canSubmit" @click="confirm">
          {{ busy ? '確認中…' : '確認' }}
        </button>
        <button v-else type="button" class="button" :disabled="!canSubmit || preview.length === 0" @click="run">
          {{ busy ? '取り込み中…' : '取り込む' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
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
  max-width: 34rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
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

.field {
  display: flex;
  flex-direction: column;
  gap: 0.375rem;
}

.field__label {
  font-size: 0.8125rem;
  font-weight: 600;
  color: var(--muted);
}

.field__required {
  margin-left: 0.5rem;
  font-weight: 400;
  color: var(--danger);
}

.field__hint {
  font-size: 0.75rem;
  line-height: 1.6;
  color: var(--muted-dim);
}

.field__hint code {
  font-size: 0.6875rem;
}

/* 貼り付ける Markdown。等幅で出して境目を読めるようにする */
.field textarea {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.75rem;
}

.modes {
  display: flex;
  flex-direction: column;
  gap: 0.375rem;
  margin: 0;
  padding: 0;
  border: none;
}

.modes legend {
  /* legend の既定の左右パディングを消して、他のラベルと左端を揃える */
  padding: 0;
}

.mode {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.8125rem;
  cursor: pointer;
}

/* グローバルの input はテキスト入力向けに width:100% + padding + border が付く。
   ラジオに当たると丸が引き伸ばされて選択肢が右へ押し出されるので、ここで戻す */
.mode input[type='radio'] {
  width: auto;
  flex-shrink: 0;
  margin: 0;
  padding: 0;
  border: none;
  border-radius: 0;
  background: none;
  accent-color: var(--accent);
}

/* 取り込む見出しの一覧 */
.preview {
  padding: 0.75rem;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
}

.preview__head {
  margin: 0 0 0.5rem;
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--muted);
}

.preview__list {
  margin: 0;
  padding-left: 1.25rem;
  max-height: 30vh;
  overflow: auto;
  font-size: 0.8125rem;
  line-height: 1.8;
}

.actions {
  display: flex;
  gap: 0.75rem;
}

.actions .button {
  flex: 1;
}

.muted {
  margin: 0;
  font-size: 0.8125rem;
  color: var(--muted);
}

@media (min-width: 40rem) {
  .modal {
    align-items: center;
  }
}
</style>
