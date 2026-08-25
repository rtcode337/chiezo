<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '@/api/client'
import { useProjectStore } from '@/stores/projects'
import { usePullToRefresh } from '@/lib/pullToRefresh'
import { useColumnFlow } from '@/lib/columnFlow'
import { buildTaskGroups, withUnlinkedGroup } from '@/lib/groups'
import { compareInProject } from '@/stores/tasks'
import ErrorBanner from '@/components/ErrorBanner.vue'
import ProjectGroups from '@/components/ProjectGroups.vue'
import TaskFormModal from '@/components/TaskFormModal.vue'
import type { Task } from '@/api/types'

/**
 * 完了したタスク。見せ方はトップの未完了一覧と同じ(ProjectGroups を共有)。
 *
 * <p>完了分はストアに持たない —— トップの未完了一覧とは寿命も更新のされ方も違うので、
 * この画面のローカル state として持ち、開くたびに取り直す。
 */
const projects = useProjectStore()

const done = ref<Task[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const editingTask = ref<Task | null>(null)

usePullToRefresh(() => load())

// 広い画面の段組み。折り返しが要らない量のときは使ったぶんの幅に絞って中央へ
const flow = useColumnFlow()

/** 完了タスクを持つプロジェクトだけ出す。トップと違って「放り込み先」の意味が無いため */
const groups = computed(() => {
  const withDone = new Set(done.value.map((t) => t.projectId))
  return withUnlinkedGroup(
    buildTaskGroups(
      done.value,
      projects.all.filter((p) => withDone.has(p.id)),
    ),
    done.value,
  )
})

async function load() {
  loading.value = true
  error.value = null
  try {
    const [items] = await Promise.all([api.listTasks({ status: 'done' }), projects.load()])
    done.value = [...items].sort(compareInProject)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)

/** 未着手に戻したものはこの一覧から消す(トップ側はストアが面倒を見る)。 */
function drop(taskId: number) {
  done.value = done.value.filter((t) => t.id !== taskId)
}
</script>

<template>
  <section class="page">
    <ErrorBanner v-if="error" :message="error" />

    <div class="head">
      <h1 class="title">
        完了したタスク <span class="count">{{ done.length }}</span>
      </h1>
    </div>

    <p v-if="loading || projects.loading" class="muted">読み込み中…</p>
    <p v-else-if="done.length === 0" class="muted">完了したタスクはありません。</p>

    <!-- 完了分は並べ替える意味が無いので sortable は付けない -->
    <div v-else :ref="flow" class="column-flow">
      <ProjectGroups
        :groups="groups"
        :sortable="false"
        :project-editable="false"
        @edit-task="editingTask = $event"
        @reopened="drop"
        @error="error = $event"
      />
    </div>

    <div class="foot">
      <RouterLink to="/" class="foot__link">← トップ</RouterLink>
    </div>

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

.count {
  font-size: 0.875rem;
  font-variant-numeric: tabular-nums;
  color: var(--muted-dim);
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

.muted {
  color: var(--muted);
}
</style>
