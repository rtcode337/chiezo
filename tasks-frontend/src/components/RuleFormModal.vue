<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRuleStore } from '@/stores/rules'
import { backdropClose } from '@/lib/backdropClose'
import type { Rule } from '@/api/types'
import ErrorBanner from '@/components/ErrorBanner.vue'

/**
 * ルールの新規作成・編集モーダル。
 * `v-if` で出し入れする前提なので、フォームの初期値は setup で一度だけ組み立てる
 * (ProjectFormModal と同じ方針)。
 */
const props = defineProps<{
  /** null = 新規作成 */
  rule: Rule | null
}>()

const emit = defineEmits<{ close: [] }>()

const backdrop = backdropClose(() => emit('close'))

const rules = useRuleStore()
const error = ref<string | null>(null)
const saving = ref(false)

const form = reactive({
  title: props.rule?.title ?? '',
  body: props.rule?.body ?? '',
})

async function save() {
  if (!form.title.trim() || !form.body.trim() || saving.value) return
  saving.value = true
  error.value = null
  try {
    // enabled は送らない: 新規はサーバー既定で有効、編集は「変更しない」扱いになる。
    // 有効/無効を動かすのは一覧のトグルだけ
    const payload = { title: form.title.trim(), body: form.body }
    if (props.rule) await rules.update(props.rule.id, payload)
    else await rules.create(payload)
    emit('close')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

async function remove() {
  const rule = props.rule
  if (!rule) return
  if (!window.confirm(`ルール「${rule.title}」を削除します。よろしいですか?`)) return
  error.value = null
  try {
    await rules.remove(rule.id)
    emit('close')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}
</script>

<template>
  <!-- 背景クリックで閉じる。中身のテキストを選択して背景で指を離したときは閉じない -->
  <div class="modal" v-on="backdrop">
    <form class="modal__panel" @submit.prevent="save">
      <div class="modal__head">
        <h2 class="modal__title">{{ rule ? 'ルールを編集' : '新しいルール' }}</h2>
        <button v-if="rule" type="button" class="delete" @click="remove">削除</button>
      </div>

      <ErrorBanner v-if="error" :message="error" />

      <label class="field">
        <span class="field__label">見出し<span class="field__required">必須</span></span>
        <span class="field__hint">連結したときの `## 見出し` になる</span>
        <input v-model="form.title" type="text" required placeholder="コミットの作法" />
      </label>

      <label class="field">
        <span class="field__label">本文<span class="field__required">必須</span></span>
        <span class="field__hint">Markdown。具体的に短く書くほど守られやすい</span>
        <textarea v-model="form.body" rows="10" required />
      </label>

      <div class="actions">
        <button type="button" class="button button--ghost" @click="emit('close')">
          キャンセル
        </button>
        <button
          type="submit"
          class="button"
          :disabled="!form.title.trim() || !form.body.trim() || saving"
        >
          {{ saving ? '保存中…' : '保存' }}
        </button>
      </div>
    </form>
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

/* 削除は見出しと同じ行の右端(タスク編集の「タスクを削除」と同じ置き方) */
.delete {
  flex-shrink: 0;
  padding: 0.25rem 0.625rem;
  border: 1px solid var(--danger);
  border-radius: 8px;
  background: transparent;
  color: var(--danger);
  font-size: 0.75rem;
  font-family: inherit;
  cursor: pointer;
  white-space: nowrap;
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
  color: var(--muted-dim);
}

.actions {
  display: flex;
  gap: 0.75rem;
}

.actions .button {
  flex: 1;
}

@media (min-width: 40rem) {
  .modal {
    align-items: center;
  }
}
</style>
