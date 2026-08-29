<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { useTaskStore } from '@/stores/tasks'
import { useProjectStore } from '@/stores/projects'
import { usePullToRefresh } from '@/lib/pullToRefresh'
import { buildTaskGroups, withUnlinkedGroup } from '@/lib/groups'
import ErrorBanner from '@/components/ErrorBanner.vue'
import ProjectFormModal from '@/components/ProjectFormModal.vue'
import ProjectGroups from '@/components/ProjectGroups.vue'
import TaskFormModal from '@/components/TaskFormModal.vue'
import TaskTransferModal from '@/components/TaskTransferModal.vue'
import type { Project, Task } from '@/api/types'

const tasks = useTaskStore()
const projects = useProjectStore()

// 下に引っ張ったら一覧を取り直す
usePullToRefresh(() => Promise.all([projects.load(true), tasks.load(true)]))

// 広い画面の段組み。折り返しが要らない量のときは使ったぶんの幅に絞って中央へ

// アーカイブ済みはここには出さない。/archived で見る。
// 出すのは未完了(done 以外)= 未着手 + 着手中。
// プロジェクト未設定のタスクは「未分類」として末尾にまとまる(0 件でも出す)
const projectGroups = computed(() =>
  withUnlinkedGroup(
    buildTaskGroups(
      tasks.active,
      projects.all.filter((p) => !p.archived),
    ),
    tasks.active,
    true,
  ),
)

const error = ref<string | null>(null)
const memo = ref('')
// null = プロジェクトに紐づけない。読み込みが済んだら先頭のプロジェクトを既定にする
const projectId = ref<number | null>(null)
const saving = ref(false)

// プロジェクトの作成・編集はこの画面から行う(専用画面は持たない)
const projectModalOpen = ref(false)
/** null = 新規作成 */
const editingProject = ref<Project | null>(null)

function openCreateProject() {
  editingProject.value = null
  projectModalOpen.value = true
}

function openEditProject(project: Project) {
  editingProject.value = project
  projectModalOpen.value = true
}

// タスクの編集はカードを押してモーダルで(専用画面は持たない)
const editingTask = ref<Task | null>(null)

// 未完了タスクのバックアップ(書き出し / 読み込み)。向きはモーダル内のタブで選ぶ
const transferOpen = ref(false)

function openEditTask(task: Task) {
  editingTask.value = task
}

onMounted(async () => {
  try {
    await Promise.all([projects.load(), tasks.load()])
    // 既定は一覧の先頭のプロジェクト(選択肢の並びと同じく「プロジェクトなし」は末尾)。
    // ほとんどのタスクはどれかのプロジェクトに属するので、毎回選ばせない。
    // 決めるのは初回の読み込みのときだけ —— 引っ張って更新するたびに
    // 選び直したプロジェクトが先頭へ戻ると、続けて入力しているあいだ邪魔になる
    projectId.value = projects.active[0]?.id ?? null
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
})

async function save() {
  const title = memo.value.trim()
  if (!title || saving.value) return
  saving.value = true
  error.value = null
  try {
    await tasks.create({ title, projectId: projectId.value ?? undefined })
    memo.value = ''
    // プロジェクト選択は次のタスクでも使い回せるよう残す
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <section class="page">
    <ErrorBanner v-if="error" :message="error" />

    <!-- 未完了のタスク。広い画面では **入力欄も同じ段の流れに入れる** ——
         そうしないと入力欄の右側が空いたままになる(入力欄の下からしか流れない) -->
    <section class="list column-flow">
      <!-- やりたいことをさっと書いて放り込む -->
      <form class="entry" @submit.prevent="save">
        <textarea
          v-model="memo"
          class="entry__input"
          rows="3"
          placeholder="やりたいことを入力 (Enter で改行、ボタンで保存)"
        />
        <div class="entry__actions">
          <select v-model="projectId" class="entry__project" aria-label="プロジェクト">
            <!-- 「プロジェクトなし」は末尾。先頭はよく使うプロジェクトの席にする -->
            <option v-for="p in projects.active" :key="p.id" :value="p.id">{{ p.name }}</option>
            <option :value="null">プロジェクトなし</option>
          </select>
          <button type="submit" class="button entry__save" :disabled="!memo.trim() || saving">
            {{ saving ? '保存中…' : '保存' }}
          </button>
        </div>
      </form>

      <h2 class="list__title">
        未完了 <span class="list__count">{{ tasks.active.length }}</span>
        <div class="list__actions">
          <!-- 持ち出しと復元。向き(書き出し / 読み込み)はモーダル内のタブで選ぶ -->
          <button type="button" class="list__action" @click="transferOpen = true">
            バックアップ
          </button>
          <button type="button" class="list__action" @click="openCreateProject">
            ＋ プロジェクト
          </button>
        </div>
      </h2>

      <!-- プロジェクトが未読込のあいだに出すと紐づき済みのタスクが消えて見えるので両方待つ -->
      <p v-if="tasks.loading || projects.loading" class="muted">読み込み中…</p>
      <p v-else-if="projectGroups.length === 0" class="muted">タスクはまだありません。</p>

      <ProjectGroups
        v-else
        :groups="projectGroups"
        @edit="openEditProject"
        @edit-task="openEditTask"
        @error="error = $event"
      />
    </section>

    <div class="foot">
      <RouterLink to="/done" class="foot__link">完了したタスク →</RouterLink>
      <RouterLink to="/archived" class="foot__link">アーカイブしたプロジェクト →</RouterLink>
    </div>

    <ProjectFormModal
      v-if="projectModalOpen"
      :project="editingProject"
      @close="projectModalOpen = false"
    />

    <TaskFormModal v-if="editingTask" :task="editingTask" @close="editingTask = null" />

    <TaskTransferModal v-if="transferOpen" @close="transferOpen = false" />
  </section>
</template>

<style scoped>
/* 広い画面では一覧に「画面の下端まで」の高さを渡す(App.vue の .app__main が起点)。
   渡さないと段組みが折り返す高さを決められない */
@media (min-width: 64rem) {
  .page {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
  }

  /* 段の途中で割れると読めなくなるものは、まとめて次の段へ送る */
  .entry,
  .list__title {
    break-inside: avoid;
  }
}

.entry {
  /* 横に長いテキストエリアは書きにくいので伸ばさない。
     広い画面では段組みの中に入る(= 段の幅)ので、これが効くのは狭い画面のほう */
  max-width: var(--reading-max);
  padding: 1rem 0 1.5rem;
}

.entry__input {
  margin-bottom: 0.5rem;
}

.entry__actions {
  display: flex;
  gap: 0.5rem;
}

.entry__project {
  flex: 1;
}

.entry__save {
  width: 6rem;
  flex-shrink: 0;
}

.list__title {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  /* ボタンが 3 つ並ぶので、狭い画面では下段へ折り返す */
  flex-wrap: wrap;
  margin: 0 0 0.75rem;
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--muted);
}

.list__count {
  font-variant-numeric: tabular-nums;
  color: var(--muted-dim);
}

/* 書き出し・読み込み・新規プロジェクトは見出しの右端から */
.list__actions {
  display: flex;
  align-items: center;
  gap: 0.375rem;
  margin-left: auto;
}

.list__action {
  padding: 0.25rem 0.625rem;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  color: var(--muted);
  font-size: 0.75rem;
  font-family: inherit;
  font-weight: 400;
  cursor: pointer;
  white-space: nowrap;
}

.list__action:hover {
  color: var(--accent);
  border-color: var(--accent);
}

/* 一番下。左は完了したタスク、右はアーカイブを戻す唯一の導線 */
.foot {
  flex-shrink: 0;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.75rem;
  margin-top: 1.5rem;
}

.foot__link,
.foot__link:visited {
  color: var(--muted-dim);
  font-size: 0.75rem;
  text-decoration: none;
}

.foot__link:hover {
  color: var(--accent);
}

.muted {
  color: var(--muted);
}
</style>
