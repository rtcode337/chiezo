<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useTaskStore } from '@/stores/tasks'
import { useProjectStore } from '@/stores/projects'
import { UNLINK_PROJECT_ID } from '@/api/types'
import { backdropClose } from '@/lib/backdropClose'
import type { Task } from '@/api/types'
import ErrorBanner from '@/components/ErrorBanner.vue'

/**
 * タスクの編集モーダル。一覧のカードを押すと開く。
 * `v-if` で出し入れする前提なので、フォームの初期値は setup で一度だけ組み立てる
 * (ProjectFormModal / RuleFormModal と同じ方針)。
 */
const props = defineProps<{ task: Task }>()

const emit = defineEmits<{ close: [] }>()

const backdrop = backdropClose(() => emit('close'))

const tasks = useTaskStore()
const projects = useProjectStore()
const error = ref<string | null>(null)
const saving = ref(false)

const form = reactive({
  projectId: props.task.projectId ?? null,
  title: props.task.title,
})

async function save() {
  if (!form.title.trim() || saving.value) return
  saving.value = true
  error.value = null
  try {
    // status は送らない(「変更しない」扱い)。状態はカードのボタンで切り替える
    await tasks.update(props.task.id, {
      // 「プロジェクトなし」は 0 で送る。undefined(= 未指定)だと「変更しない」になり紐づけが外れない
      projectId: form.projectId ?? UNLINK_PROJECT_ID,
      title: form.title.trim(),
    })
    emit('close')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

async function remove() {
  if (!window.confirm('このタスクを削除します。よろしいですか?')) return
  error.value = null
  try {
    await tasks.remove(props.task.id)
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
        <h2 class="modal__title">タスクを編集</h2>
        <!-- 削除は誤タップを避けるため一覧には置かず、ここからだけ行う -->
        <button type="button" class="delete" @click="remove">削除</button>
      </div>

      <ErrorBanner v-if="error" :message="error" />

      <label class="field">
        <span class="field__label">タスク内容<span class="field__required">必須</span></span>
        <textarea v-model="form.title" rows="4" required placeholder="やりたいこと" />
      </label>

      <label class="field">
        <span class="field__label">プロジェクト</span>
        <select v-model="form.projectId">
          <!-- 並びはトップの入力欄と同じ(「プロジェクトなし」は末尾)。
               既定はそのタスクの今のプロジェクトなので、こちらは先頭を選ばない -->
          <option v-for="project in projects.active" :key="project.id" :value="project.id">
            {{ project.name }}
          </option>
          <option :value="null">プロジェクトなし</option>
        </select>
      </label>

      <div class="actions">
        <button type="button" class="button button--ghost" @click="emit('close')">
          キャンセル
        </button>
        <button type="submit" class="button" :disabled="!form.title.trim() || saving">
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
  max-width: 30rem;
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

/* 削除は見出しと同じ行の右端 */
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
