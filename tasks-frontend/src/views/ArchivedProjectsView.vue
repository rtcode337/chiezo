<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { useTaskStore } from '@/stores/tasks'
import { useProjectStore } from '@/stores/projects'
import { usePullToRefresh } from '@/lib/pullToRefresh'
import { buildTaskGroups } from '@/lib/groups'
import ErrorBanner from '@/components/ErrorBanner.vue'
import ProjectFormModal from '@/components/ProjectFormModal.vue'
import ProjectGroups from '@/components/ProjectGroups.vue'
import TaskFormModal from '@/components/TaskFormModal.vue'
import type { Project, Task } from '@/api/types'

/**
 * アーカイブしたプロジェクトの一覧。見え方はトップと同じ(ProjectGroups を共有)。
 * ここが「アーカイブから戻す」唯一の導線になるので、消すときは代わりを用意すること。
 */
const tasks = useTaskStore()
const projects = useProjectStore()

usePullToRefresh(() => Promise.all([projects.load(true), tasks.load(true)]))

// 広い画面の段組み。折り返しが要らない量のときは使ったぶんの幅に絞って中央へ

const error = ref<string | null>(null)
const editingProject = ref<Project | null>(null)
const modalOpen = ref(false)

// アーカイブ済みは並べ替える意味が無いので sortable は付けない
const groups = computed(() =>
  buildTaskGroups(
    tasks.active,
    projects.all.filter((p) => p.archived),
  ),
)

onMounted(async () => {
  try {
    await Promise.all([projects.load(), tasks.load()])
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
})

function openEdit(project: Project) {
  editingProject.value = project
  modalOpen.value = true
}

const editingTask = ref<Task | null>(null)
</script>

<template>
  <section class="page">
    <ErrorBanner v-if="error" :message="error" />

    <div class="head">
      <h1 class="title">アーカイブしたプロジェクト</h1>
    </div>

    <p v-if="tasks.loading || projects.loading" class="muted">読み込み中…</p>
    <p v-else-if="groups.length === 0" class="muted">アーカイブしたプロジェクトはありません。</p>

    <template v-else>
      <p class="hint">「編集」から <strong>アーカイブから戻す</strong> と、トップに戻ります。</p>
      <div class="column-flow">
        <ProjectGroups
          :groups="groups"
          :sortable="false"
          @edit="openEdit"
          @edit-task="editingTask = $event"
          @error="error = $event"
        />
      </div>
    </template>

    <div class="foot">
      <RouterLink to="/" class="foot__link">← トップ</RouterLink>
    </div>

    <ProjectFormModal
      v-if="modalOpen"
      :project="editingProject"
      @close="modalOpen = false"
    />

    <TaskFormModal v-if="editingTask" :task="editingTask" @close="editingTask = null" />
  </section>
</template>

<style scoped>
/* 広い画面では一覧に「画面の下端まで」の高さを渡す(トップと同じ。App.vue 参照) */
@media (min-width: 64rem) {
  .page {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
  }
}

.head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.75rem;
  padding: 1rem 0;
}

.title {
  margin: 0;
  font-size: 1.125rem;
}

/* トップへ戻る導線は一覧の左下(トップから来るときのリンクと同じ位置・同じ見た目) */
.foot {
  flex-shrink: 0;
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

.hint {
  margin: 0 0 0.75rem;
  font-size: 0.75rem;
  color: var(--muted-dim);
}

.muted {
  color: var(--muted);
}
</style>
