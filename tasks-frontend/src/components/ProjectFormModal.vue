<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useProjectStore } from '@/stores/projects'
import { useTaskStore } from '@/stores/tasks'
import { backdropClose } from '@/lib/backdropClose'
import type { Project } from '@/api/types'
import ErrorBanner from '@/components/ErrorBanner.vue'

/**
 * プロジェクトの新規作成・編集モーダル。
 * トップ(新規のみ)とプロジェクト画面(新規・編集)で共有する。
 * v-if で出し入れする前提なので、フォームの初期値は setup で一度作れば足りる。
 */
const props = defineProps<{
  /** null = 新規作成 */
  project: Project | null
}>()

const emit = defineEmits<{ close: []; saved: [project: Project] }>()

const backdrop = backdropClose(() => emit('close'))

const projects = useProjectStore()
const tasks = useTaskStore()
const error = ref<string | null>(null)
const saving = ref(false)

const form = reactive({
  name: props.project?.name ?? '',
  repoUrls: props.project && props.project.repoUrls.length > 0 ? [...props.project.repoUrls] : [''],
  description: props.project?.description ?? '',
})

/**
 * アーカイブは **未完了が 0 件のときだけ**通す(サーバー側でも弾いている)。
 * 片付いていないタスクごとトップから消えると、放り込んだものを取りこぼすため。
 * 戻すのはいつでもよい。
 */
const todoCount = computed(() =>
  props.project ? tasks.active.filter((t) => t.projectId === props.project?.id).length : 0,
)
const canArchive = computed(() => props.project?.archived || todoCount.value === 0)

function addRepoUrl() {
  form.repoUrls.push('')
}

function removeRepoUrl(index: number) {
  form.repoUrls.splice(index, 1)
  if (form.repoUrls.length === 0) form.repoUrls.push('')
}

/** URL の末尾 user/repo 形からリポジトリ名を取り出す。取れなければ空文字。 */
function repoNameFromUrl(url: string): string {
  const trimmed = url
    .trim()
    .replace(/\/+$/, '')
    .replace(/\.git$/, '')
  const match = trimmed.match(/[/:]([^/:]+)\/([^/:]+)$/)
  return match ? match[2] : ''
}

/** 最初の URL を入れ終えたとき、名前が空ならリポジトリ名を自動セットする */
function onFirstRepoUrlChange() {
  if (form.name.trim()) return
  const name = repoNameFromUrl(form.repoUrls[0] ?? '')
  if (name) form.name = name
}

/** アーカイブ切り替えも「入力中の内容ごと保存する」。編集を捨てさせない */
function payload(archived?: boolean) {
  return {
    name: form.name.trim(),
    repoUrls: form.repoUrls.map((u) => u.trim()).filter((u) => u !== ''),
    description: form.description,
    ...(archived === undefined ? {} : { archived }),
  }
}

async function save() {
  if (!form.name.trim() || saving.value) return
  saving.value = true
  error.value = null
  try {
    const saved = props.project
      ? await projects.update(props.project.id, payload())
      : await projects.create(payload())
    emit('saved', saved)
    emit('close')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

/**
 * 削除はアーカイブ済みのときだけ。アーカイブ自体が「未完了 0 件」を条件にしているので、
 * 片付いたことを確かめる一段を必ず通ってから消える(サーバー側でも弾いている)。
 */
async function remove() {
  const project = props.project
  if (!project || saving.value) return
  // 件数は出さない —— ストアには未完了しか無く、アーカイブ済みは必ず 0 件なので
  // 「0 件」と言いながら完了済みを消すことになる
  const message =
    `「${project.name}」を削除します。\n` +
    '完了したものを含め、紐づくタスクもすべて消えます。戻せません。よろしいですか?'
  if (!window.confirm(message)) return
  saving.value = true
  error.value = null
  try {
    await projects.remove(project.id)
    tasks.dropByProject(project.id)
    emit('close')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

async function toggleArchive() {
  const project = props.project
  if (!project || saving.value || !form.name.trim() || !canArchive.value) return
  saving.value = true
  error.value = null
  try {
    const saved = await projects.update(project.id, payload(!project.archived))
    emit('saved', saved)
    emit('close')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <!-- 背景クリックで閉じる。中身のテキストを選択して背景で指を離したときは閉じない -->
  <div class="modal" v-on="backdrop">
    <form class="modal__panel" @submit.prevent="save">
      <div class="modal__head">
        <h2 class="modal__title">{{ project ? 'プロジェクトを編集' : '新しいプロジェクト' }}</h2>
        <!-- アーカイブは見出しと同じ行の右端(タスク編集の「タスクを削除」と同じ置き方) -->
        <div v-if="project" class="modal__tools">
          <button
            type="button"
            class="archive"
            :class="{ 'archive--restore': project.archived }"
            :disabled="!canArchive || saving"
            :title="canArchive ? undefined : '未完了のタスクが残っています'"
            @click="toggleArchive"
          >
            {{ project.archived ? 'アーカイブから戻す' : 'アーカイブする' }}
          </button>
          <!-- 削除はアーカイブ済みのときだけ。戻せない操作なので一番右端に置く -->
          <button
            v-if="project.archived"
            type="button"
            class="archive archive--delete"
            :disabled="saving"
            @click="remove"
          >
            削除
          </button>
        </div>
      </div>
      <p v-if="project && !canArchive" class="archive__note">
        未完了が {{ todoCount }} 件あるためアーカイブできません。片付けてから。
      </p>

      <ErrorBanner v-if="error" :message="error" />

      <div class="field">
        <span class="field__label">リポジトリ URL</span>
        <span class="field__hint">複数登録できる。最初の URL から名前を自動入力する</span>
        <div v-for="(_, i) in form.repoUrls" :key="i" class="repo-row">
          <input
            v-model="form.repoUrls[i]"
            type="url"
            placeholder="https://github.com/..."
            @change="i === 0 && onFirstRepoUrlChange()"
          />
          <button
            v-if="form.repoUrls.length > 1 || form.repoUrls[0] !== ''"
            type="button"
            class="repo-row__remove"
            aria-label="この URL を削除"
            @click="removeRepoUrl(i)"
          >
            ×
          </button>
        </div>
        <button type="button" class="repo-add" @click="addRepoUrl">＋ URL を追加</button>
      </div>

      <label class="field">
        <span class="field__label">名前<span class="field__required">必須</span></span>
        <span class="field__hint">タスクのグループ名。リポジトリ名に揃えると迷わない</span>
        <input v-model="form.name" type="text" required placeholder="sample-project" />
      </label>

      <label class="field">
        <span class="field__label">説明</span>
        <textarea v-model="form.description" rows="2" />
      </label>

      <div class="actions">
        <button type="button" class="button button--ghost" @click="emit('close')">
          キャンセル
        </button>
        <button type="submit" class="button" :disabled="!form.name.trim() || saving">
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

.modal__tools {
  display: flex;
  align-items: center;
  gap: 0.375rem;
  flex-shrink: 0;
}

.archive {
  flex-shrink: 0;
  padding: 0.25rem 0.625rem;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  color: var(--muted);
  font-size: 0.75rem;
  font-family: inherit;
  cursor: pointer;
  white-space: nowrap;
}

.archive:hover:not(:disabled) {
  color: var(--danger);
  border-color: var(--danger);
}

.archive--delete {
  color: var(--danger);
  border-color: var(--danger);
}

/* 戻す方は「元に戻す」なので危険色にしない */
.archive--restore:hover:not(:disabled) {
  color: var(--accent);
  border-color: var(--accent);
}

.archive:disabled {
  opacity: 0.4;
  cursor: default;
}

.archive__note {
  margin: -0.5rem 0 0;
  font-size: 0.75rem;
  color: var(--muted-dim);
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

.repo-row {
  display: flex;
  align-items: center;
  gap: 0.375rem;
}

.repo-row input {
  flex: 1;
  min-width: 0;
}

.repo-row__remove {
  background: none;
  border: none;
  color: var(--muted-dim);
  font-size: 1rem;
  line-height: 1;
  padding: 0.25rem;
  cursor: pointer;
}

.repo-row__remove:hover {
  color: var(--danger);
}

.repo-add {
  align-self: flex-start;
  background: none;
  border: none;
  padding: 0;
  color: var(--accent);
  font: inherit;
  font-size: 0.8125rem;
  cursor: pointer;
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
