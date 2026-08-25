<script setup lang="ts">
import { computed, ref } from 'vue'
import { useTaskStore } from '@/stores/tasks'
import { useProjectStore } from '@/stores/projects'
import { useDragSort } from '@/lib/dragSort'
import { moveTaskToProject } from '@/lib/taskMove'
import { groupProjectId, isProjectGroup } from '@/lib/groups'
import TaskCard from '@/components/TaskCard.vue'
import type { TaskGroup } from '@/lib/groups'
import type { Project, Task } from '@/api/types'

/**
 * タスクをプロジェクトごとに折りたたんで並べる。
 * トップ・アーカイブ一覧・完了一覧で同じ見え方にするため、ここに寄せている。
 *
 * 「未分類」(project が null のグループ)も同じ見た目で描くが、見出しの並び替えと
 * 編集だけは持たない —— プロジェクトではないので動かす先も編集する中身も無いため。
 */
const props = withDefaults(
  defineProps<{
    groups: TaskGroup[]
    /** 長押しドラッグで並び替えられるか。アーカイブ一覧では並べ替える意味が無いので false */
    sortable?: boolean
    /** 見出しにプロジェクトの「編集」を出すか。完了一覧では場違いなので false */
    projectEditable?: boolean
  }>(),
  { sortable: true, projectEditable: true },
)

const emit = defineEmits<{
  edit: [project: Project]
  editTask: [task: Task]
  /** 未着手に戻した。完了済み一覧が自分の手元から取り除くために使う */
  reopened: [taskId: number]
  error: [message: string]
}>()

const tasks = useTaskStore()
const projects = useProjectStore()

// 折りたたみ状態。デフォルトは閉じた状態で、開いたグループだけをブラウザに永続化する
const EXPANDED_KEY = 'chiezo-tasks-home-expanded'

function loadExpanded(): Set<string> {
  try {
    const raw = localStorage.getItem(EXPANDED_KEY)
    return new Set(raw ? (JSON.parse(raw) as string[]) : [])
  } catch {
    return new Set()
  }
}

const expanded = ref(loadExpanded())

function toggleGroup(key: string) {
  const next = new Set(expanded.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  expanded.value = next
  try {
    localStorage.setItem(EXPANDED_KEY, JSON.stringify([...next]))
  } catch {
    // 保存できなくてもその場の開閉は効くので握りつぶす
  }
}

function fail(e: unknown) {
  emit('error', e instanceof Error ? e.message : String(e))
}

// 見出しの長押しドラッグでプロジェクトの並び順そのものが変わる
// (ここに出ていないプロジェクトは元の位置に据え置かれる)
const projectSorter = useDragSort<TaskGroup>(async (_key, ordered) => {
  try {
    await projects.reorderVisible(ordered.filter(isProjectGroup).map((g) => g.project))
  } catch (e) {
    fail(e)
  }
})

/** 見出しを動かせるのはプロジェクトのグループだけ */
const sortableGroups = computed(() => props.groups.filter(isProjectGroup))
/** 未分類は動かさず、常に末尾に置く */
const pinnedGroups = computed(() => props.groups.filter((g) => !isProjectGroup(g)))
const rendered = computed(() => [
  ...projectSorter.view('projects', sortableGroups.value),
  ...pinnedGroups.value,
])

/**
 * カードの長押しドラッグ。基本はそのグループの中の並び替えだが、
 * 別のプロジェクトのグループまで運んだらそのプロジェクトへ移す。
 */
const taskSorter = useDragSort<Task>(
  async (key, ordered, drop) => {
    try {
      // 別のグループへ落としたなら移動。移動できなければ元のグループの並び替えとして扱う
      if (drop && (await moveTaskToProject(drop, props.groups))) return
      await tasks.reorder(
        groupProjectId(key),
        ordered.map((t) => t.id),
      )
    } catch (e) {
      fail(e)
    }
  },
  { dropZones: true },
)

async function complete(id: number) {
  try {
    await tasks.complete(id)
  } catch (e) {
    fail(e)
  }
}

/** 着手トグル。着手(todo → in_progress)と未着手に戻す(in_progress → todo)。 */
async function setStatus(id: number, status: 'todo' | 'in_progress') {
  try {
    await tasks.update(id, { status })
  } catch (e) {
    fail(e)
  }
}

/** 「修正が大変そう」の印の付け外し。カードの面の色だけが変わる(並びには効かせない)。 */
async function toggleFlag(task: Task) {
  try {
    await tasks.update(task.id, { flagged: !task.flagged })
  } catch (e) {
    fail(e)
  }
}

/** 完了済み一覧から未着手に戻す。 */
async function reopen(id: number) {
  try {
    await tasks.update(id, { status: 'todo' })
    emit('reopened', id)
  } catch (e) {
    fail(e)
  }
}

/** 並び替えを無効にしたいとき、および末尾の未分類では pointerdown を握らない */
function startProject(index: number, event: PointerEvent) {
  if (!props.sortable || index >= sortableGroups.value.length) return
  projectSorter.start('projects', sortableGroups.value, index, '.groups', event)
}

function startTask(group: TaskGroup, index: number, event: PointerEvent) {
  if (props.sortable) taskSorter.start(group.key, group.tasks, index, '.cards', event)
}
</script>

<template>
  <div class="groups">
    <!-- data-drop-zone: 他のリストから運んできたタスクの落下先。group--drop で枠が付く -->
    <section
      v-for="(group, gi) in rendered"
      :key="group.key"
      class="group"
      :class="{
        'group--dragging': projectSorter.isDragging('projects', gi),
        'group--drop': taskSorter.dropZone() === group.key,
      }"
      :data-drop-zone="group.key"
      @pointerdown="startProject(gi, $event)"
      @click.capture="projectSorter.clickGuard"
    >
      <div class="group__head">
        <button
          type="button"
          class="group__toggle"
          :aria-expanded="expanded.has(group.key)"
          @click="toggleGroup(group.key)"
        >
          <span
            class="group__chevron"
            :class="{ 'group__chevron--open': expanded.has(group.key) }"
            >▸</span
          >
          <span class="group__name">{{ group.project?.name ?? '未分類' }}</span>
          <span class="group__count">{{ group.tasks.length }}</span>
        </button>
        <!-- data-no-drag: 見出しの長押しはドラッグなので、ここだけは奪わない -->
        <button
          v-if="projectEditable && group.project"
          type="button"
          class="group__edit"
          data-no-drag
          @click="group.project && emit('edit', group.project)"
        >
          編集
        </button>
      </div>

      <div v-show="expanded.has(group.key)">
        <ul v-if="group.tasks.length > 0" class="cards">
          <li
            v-for="(task, i) in taskSorter.view(group.key, group.tasks)"
            :key="task.id"
            class="item"
            :class="{ 'item--dragging': taskSorter.isDragging(group.key, i) }"
            @pointerdown.stop="startTask(group, i, $event)"
            @click.capture="taskSorter.clickGuard"
          >
            <TaskCard
              :task="task"
              :repo-urls="group.project?.repoUrls"
              @edit="emit('editTask', task)"
              @complete="complete(task.id)"
              @reopen="reopen(task.id)"
              @start="setStatus(task.id, 'in_progress')"
              @unstart="setStatus(task.id, 'todo')"
              @toggle-flag="toggleFlag(task)"
            />
          </li>
        </ul>
        <p v-else class="group__empty">まだタスクはありません。</p>
      </div>
    </section>
  </div>
</template>

<style scoped>
.groups {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

/* 運んできたタスクの落下先。ここで離すとこのプロジェクトへ移る。
   枠は outline で描く —— border だと幅の分だけレイアウトがずれて、
   追従中のカードの位置がドラッグ中に飛んでしまうため */
.group--drop {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: 10px;
}

.group--dragging {
  opacity: 0.9;
}

.group--dragging .group__name {
  color: var(--accent);
}

.group__head {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin: 0 0 0.375rem;
}

.group__toggle {
  display: flex;
  align-items: center;
  gap: 0.375rem;
  flex: 1;
  min-width: 0;
  padding: 0.25rem 0;
  border: none;
  background: none;
  font-family: inherit;
  font-size: 0.8125rem;
  font-weight: 600;
  color: var(--muted);
  cursor: pointer;
  text-align: left;
  /* 長押しでドラッグを始めるので、iOS の長押しメニューと文字選択は出さない */
  -webkit-touch-callout: none;
  user-select: none;
}

.group__toggle:hover .group__name {
  color: var(--text);
}

.group__chevron {
  display: inline-block;
  transition: transform 0.15s ease;
  color: var(--muted-dim);
}

.group__chevron--open {
  transform: rotate(90deg);
}

.group__name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.group__count {
  font-variant-numeric: tabular-nums;
  font-weight: 400;
  color: var(--muted-dim);
}

/* 見出しの右端。プロジェクトの編集・アーカイブはここから */
.group__edit {
  flex-shrink: 0;
  padding: 0.125rem 0.5rem;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  color: var(--muted);
  font-size: 0.6875rem;
  font-family: inherit;
  cursor: pointer;
  white-space: nowrap;
}

.group__edit:hover {
  color: var(--accent);
  border-color: var(--accent);
}

.group__empty {
  margin: 0;
  padding: 0.125rem 0 0.375rem 1.25rem;
  font-size: 0.75rem;
  color: var(--muted-dim);
}

.cards {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

/* 広い画面では段組み(.column-flow。main.css)の中に流し込まれる。**段組み自体はここでは
   組まない** —— トップでは入力欄も同じ流れに入れる(入力欄の右にもカードが回り込む)ので、
   段組みの箱はビュー側が持つ。ここでやるのは「段をまたいで流せる形」にすることだけ:
   flex コンテナは段をまたいで分割できないので block に戻し、gap ではなく margin で間隔を取る。
   スマホ(1 段しか入らない幅)は 1 列のまま変えない。 */
@media (min-width: 64rem) {
  .groups {
    display: block;
  }

  .group {
    margin-bottom: 0.75rem;
  }

  /* 見出しだけが段の末尾に取り残されると、どのグループの続きか読めなくなる */
  .group__head {
    break-after: avoid;
  }

  .cards {
    display: block;
  }

  .item {
    /* カードは途中で切らない(切れると 1 枚が 2 段に割れて読めない) */
    break-inside: avoid;
  }

  .item + .item {
    margin-top: 0.5rem;
  }
}

/* 掴んでいる行。持ち上がって見えるようにする(掴み代が無いぶん状態を分かりやすく) */
.item--dragging :deep(.card) {
  opacity: 0.9;
  border-color: var(--accent);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.18);
}
</style>
