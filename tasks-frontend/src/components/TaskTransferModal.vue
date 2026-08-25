<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useTaskStore } from '@/stores/tasks'
import { useProjectStore } from '@/stores/projects'
import { backdropClose } from '@/lib/backdropClose'
import { stampedName } from '@/lib/fileTransfer'
import type { TaskExport, TaskImportResult } from '@/api/types'
import ErrorBanner from '@/components/ErrorBanner.vue'
import CopyButton from '@/components/CopyButton.vue'
import HintTip from '@/components/HintTip.vue'
import FileLoadButton from '@/components/FileLoadButton.vue'
import FileSaveButton from '@/components/FileSaveButton.vue'

/**
 * 未完了タスクのバックアップ(書き出し / 読み込み)。DB を失っても打ち直さずに
 * 戻せるようにするための機能。**書き出したものをそのまま読み込める**ので、
 * 手元に置いたテキストやファイルがバックアップになる。
 *
 * **両方向を 1 つのモーダルにタブで収める。** 入口はトップの見出しに 1 つだけ置きたく
 * (めったに押さないものにボタンを 2 つ割かない)、開いてから向きを選ぶほうが
 * 「書き出したものをそのまま読み込む」関係も見えるため。
 *
 * 何を作り何を飛ばすかの判断はサーバー側(`TaskTransferService`)に任せ、
 * 画面は貼られた文字列を JSON として解釈するところまでを持つ。
 */
const emit = defineEmits<{ close: [] }>()

const backdrop = backdropClose(() => emit('close'))

const tasks = useTaskStore()
const projects = useProjectStore()
const error = ref<string | null>(null)
const busy = ref(false)

/** 開いた直後は書き出し(バックアップを取るほうが日常的な用) */
const tab = ref<'export' | 'import'>('export')

// --- 書き出し ---
const exported = ref('')

// --- 読み込み ---
const input = ref('')
/** dryRun の結果。null = まだ確認していない */
const preview = ref<TaskImportResult | null>(null)

/** 書き出しの本文を取る。一度取ったら使い回す(タブを往復するたびに叩かない) */
async function loadExport() {
  if (exported.value || busy.value) return
  busy.value = true
  try {
    // 貼り付けやすいよう整形する。この文字列をそのまま読み込みに渡せる
    exported.value = JSON.stringify(await tasks.exportAll(), null, 2)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

onMounted(loadExport)

/** タブの切り替え。エラーは向きごとの話なので持ち越さない */
function select(next: 'export' | 'import') {
  if (tab.value === next) return
  tab.value = next
  error.value = null
  if (next === 'export') loadExport()
}

/** 貼られた文字列を JSON として読む。壊れていれば読み込みに行かず手元で弾く */
function parse(): TaskExport | null {
  try {
    return JSON.parse(input.value) as TaskExport
  } catch {
    error.value = 'JSON として読めません。書き出した内容をそのまま貼り付けてください'
    return null
  }
}

/** 本文を変えたら、確認済みの内容は古くなるので破棄する */
function invalidatePreview() {
  preview.value = null
}

/** 選ばれたファイルの中身を入力欄へ流し込む(取り込みは走らせない。貼ったときと同じ流れ) */
function loadFile(text: string) {
  input.value = text
  error.value = null
  invalidatePreview()
}

async function confirm() {
  if (busy.value) return
  error.value = null
  const data = parse()
  if (!data) return
  busy.value = true
  try {
    preview.value = await tasks.importAll(data, true)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function run() {
  if (busy.value || !preview.value) return
  error.value = null
  const data = parse()
  if (!data) return
  busy.value = true
  try {
    await tasks.importAll(data)
    // プロジェクトが増えていることがあるので一覧を取り直す
    await projects.load(true)
    emit('close')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <!-- 背景クリックで閉じる。本文を選択して背景で指を離したときは閉じない -->
  <div class="modal" v-on="backdrop">
    <div class="modal__panel">
      <div class="modal__head">
        <h2 class="modal__title">タスクのバックアップ</h2>
        <button type="button" class="icon-button" aria-label="閉じる" title="閉じる" @click="emit('close')">
          <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
            <path
              d="M6 6l12 12M18 6L6 18"
              fill="none"
              stroke="currentColor"
              stroke-width="1.7"
              stroke-linecap="round"
            />
          </svg>
        </button>
      </div>

      <!-- 向きの切り替え。書き出したものをそのまま読み込めるので、同じモーダルに並べる -->
      <div class="tabs" role="tablist">
        <button
          type="button"
          class="tab"
          :class="{ 'tab--on': tab === 'export' }"
          role="tab"
          :aria-selected="tab === 'export'"
          @click="select('export')"
        >
          書き出し
        </button>
        <button
          type="button"
          class="tab"
          :class="{ 'tab--on': tab === 'import' }"
          role="tab"
          :aria-selected="tab === 'import'"
          @click="select('import')"
        >
          読み込み
        </button>
      </div>

      <ErrorBanner v-if="error" :message="error" />

      <!-- 書き出し -->
      <template v-if="tab === 'export'">
        <p v-if="busy" class="muted">書き出し中…</p>
        <div v-else class="field">
          <!-- 見出しと同じ行にファイル。コピーは枠の中(右上)に重ねる -->
          <div class="field__head">
            <span class="field__label">
              書き出す内容
              <HintTip>
                未完了のタスクを、所属プロジェクトの名前とリポジトリ URL 付きで書き出す。
                コピーかファイルで保存しておけば、その内容をそのまま「読み込み」に戻せる。
                完了タスクとプロジェクトの説明は含まない。
              </HintTip>
            </span>
            <FileSaveButton
              v-if="exported"
              :text="exported"
              :filename="stampedName('chiezo-tasks', 'json')"
              mime="application/json"
            />
          </div>
          <div class="box">
            <pre class="dump">{{ exported }}</pre>
            <CopyButton
              v-if="exported"
              icon
              class="box__copy"
              :text="exported"
              label="書き出した内容をコピー"
            />
          </div>
        </div>
      </template>

      <!-- 読み込み -->
      <template v-else>
        <div class="field">
          <!-- 見出しと同じ列に置く。貼り付けとファイルは同じ「入力の入れ方」なので並べる -->
          <div class="field__head">
            <span class="field__label">
              <label for="transfer-input">書き出した内容</label>
              <HintTip>
                プロジェクトは名前で照合し、無ければ作る(リポジトリ URL も一緒に登録する)。
                既にあるプロジェクトは触らない。
                同じタイトルの未完了タスクが既にあれば飛ばすので、二度読み込んでも増えない。
              </HintTip>
              <span class="field__required">必須</span>
            </span>
            <FileLoadButton
              accept=".json,application/json,text/plain"
              @load="loadFile"
              @error="error = $event"
            />
          </div>
          <textarea
            id="transfer-input"
            v-model="input"
            rows="10"
            required
            placeholder='{ "version": 1, "projects": [ … ] }'
            @input="invalidatePreview"
          />
        </div>

        <!-- 何が入るかを先に見せる -->
        <div v-if="preview" class="preview">
          <p v-if="preview.createdProjects.length" class="preview__head">
            作るプロジェクト ({{ preview.createdProjects.length }})
          </p>
          <ul v-if="preview.createdProjects.length" class="preview__list">
            <li v-for="name in preview.createdProjects" :key="name">{{ name }}</li>
          </ul>

          <p class="preview__head">作るタスク ({{ preview.createdTasks.length }})</p>
          <ul v-if="preview.createdTasks.length" class="preview__list">
            <li v-for="label in preview.createdTasks" :key="label">{{ label }}</li>
          </ul>
          <p v-else class="muted">新しく作るタスクはありません。</p>

          <template v-if="preview.skippedTasks.length">
            <p class="preview__head">既にあるので飛ばす ({{ preview.skippedTasks.length }})</p>
            <ul class="preview__list preview__list--muted">
              <li v-for="label in preview.skippedTasks" :key="label">{{ label }}</li>
            </ul>
          </template>
        </div>
      </template>

      <!-- 閉じるは右上の × に寄せてあるので、ここは読み込みの実行だけ -->
      <div v-if="tab === 'import'" class="actions">
        <button
          v-if="!preview"
          type="button"
          class="button"
          :disabled="!input.trim() || busy"
          @click="confirm"
        >
          {{ busy ? '確認中…' : '確認' }}
        </button>
        <button
          v-else
          type="button"
          class="button"
          :disabled="busy || preview.createdTasks.length === 0"
          @click="run"
        >
          {{ busy ? '読み込み中…' : '読み込む' }}
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

/* 見出しの行。左が見出し(+ 説明の「?」)、右端がファイルのボタン */
.field__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
}

/* 書き出し / 読み込みの切り替え。下線で選択を出す ——
   明テーマでは --surface と --surface-raised が同色なので、面の塗り分けでは選択が見えない */
.tabs {
  display: flex;
  gap: 1rem;
  margin-bottom: -0.25rem;
  border-bottom: 1px solid var(--border);
}

.tab {
  margin-bottom: -1px;
  padding: 0.375rem 0.125rem;
  border: none;
  border-bottom: 2px solid transparent;
  background: none;
  color: var(--muted);
  font-size: 0.875rem;
  font-family: inherit;
  cursor: pointer;
}

.tab--on {
  color: var(--accent);
  border-bottom-color: var(--accent);
}

/* コピーを枠の中の右上に重ねるための入れ物。スクロールするのは中の .dump なので、
   ボタンは流れず角に留まる */
.box {
  position: relative;
}

.box__copy {
  position: absolute;
  top: 0.5rem;
  right: 0.5rem;
}

/* 書き出した素の JSON。整形せずそのまま出す(コピーするのはこれ自体だから)。
   右の余白はコピーのぶん(文字がボタンの下に潜らないように) */
.dump {
  margin: 0;
  max-height: 55vh;
  overflow: auto;
  padding: 0.75rem;
  padding-right: 3rem;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
  font-size: 0.75rem;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 0.375rem;
}

/* 見出し。説明の「?」と必須の印を横に並べる */
.field__label {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.8125rem;
  font-weight: 600;
  color: var(--muted);
}

.field__required {
  font-weight: 400;
  color: var(--danger);
}

.field textarea {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.75rem;
}

.preview {
  max-height: 40vh;
  overflow: auto;
  padding: 0.75rem;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
}

.preview__head {
  margin: 0.75rem 0 0.375rem;
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--muted);
}

.preview__head:first-child {
  margin-top: 0;
}

.preview__list {
  margin: 0;
  padding-left: 1.25rem;
  font-size: 0.8125rem;
  line-height: 1.7;
}

.preview__list--muted {
  color: var(--muted-dim);
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
