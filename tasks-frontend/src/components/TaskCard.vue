<script setup lang="ts">
import CopyButton from '@/components/CopyButton.vue'
import ClaudeCodeButton from '@/components/ClaudeCodeButton.vue'
import type { Task } from '@/api/types'

/** トップの未完了一覧のカード。未紐づけ・プロジェクトどちらのリストからも使う。 */
defineProps<{ task: Task; repoUrls?: string[] }>()

/**
 * start = 着手(todo → in_progress)、unstart = 未着手に戻す(in_progress → todo)、
 * toggleFlag = 「修正が大変そう」の印の付け外し
 */
const emit = defineEmits<{
  edit: []
  complete: []
  reopen: []
  start: []
  unstart: []
  toggleFlag: []
}>()
</script>

<template>
  <!-- 本文を押すと編集モーダル。専用の編集ボタンは置かない -->
  <div class="card" :class="{ 'card--flagged': task.flagged }">
    <button type="button" class="card__memo" @click="emit('edit')">{{ task.title }}</button>

    <!-- 右カラム。メモが複数行なら伸びて、ボタンの下端がメモの下端に揃う -->
    <div class="card__side">
      <!-- data-no-drag: ここの長押しはドラッグにしない。特に ✳ は iOS で
           「長押し → Safari で開く」を選ばせる必要があるので奪ってはいけない -->
      <span class="card__tools" data-no-drag>
        <!-- 「修正が大変そう」の印。押すたびに付け外しする(状態は面の色で見せる) -->
        <button
          type="button"
          class="icon-button flag"
          :class="{ 'flag--on': task.flagged }"
          :aria-pressed="task.flagged"
          :aria-label="task.flagged ? '修正が大変そうの印を外す' : '修正が大変そうの印を付ける'"
          :title="task.flagged ? '修正が大変そう(押すと外す)' : '修正が大変そう'"
          @click.stop.prevent="emit('toggleFlag')"
        >
          <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
            <path d="M6 3.5V21" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
            <path
              d="M6 4.5h11l-2.6 3.6L17 11.7H6z"
              :fill="task.flagged ? 'currentColor' : 'none'"
              stroke="currentColor"
              stroke-width="1.7"
              stroke-linejoin="round"
            />
          </svg>
        </button>
        <CopyButton icon :text="task.title" />
        <ClaudeCodeButton :task="task" :repo-urls="repoUrls" />
      </span>
      <span class="card__buttons" data-no-drag>
        <!-- 完了済みの一覧では同じ位置が「戻す」になる -->
        <button v-if="task.status === 'done'" type="button" class="btn" @click="emit('reopen')">
          未着手に戻す
        </button>
        <template v-else>
          <!-- 着手トグル。「依頼はしたが動作確認が済んでいない」を着手中で表す。
               着手中は色付きで、もう一度押すと未着手に戻る -->
          <button
            v-if="task.status === 'in_progress'"
            type="button"
            class="btn btn--started"
            title="未着手に戻す"
            @click="emit('unstart')"
          >
            着手中
          </button>
          <button v-else type="button" class="btn btn--start" @click="emit('start')">着手</button>
          <button type="button" class="btn btn--done" @click="emit('complete')">完了</button>
        </template>
      </span>
    </div>
  </div>
</template>

<style scoped>
.card {
  display: flex;
  /* 右カラムをメモの高さまで伸ばすため stretch */
  align-items: stretch;
  gap: 0.625rem;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
  padding: 0.75rem;
  /* 長押しでドラッグを始めるので、iOS のリンクプレビュー等の長押しメニューは出さない */
  -webkit-touch-callout: none;
}

/* 修正が大変そうな印。面を水色に塗って、一覧を流し読みしていても目に留まるようにする
   (着手中の色付きボタンとは別軸なので、色は状態バッジのどれとも被らない水色にしてある) */
.card--flagged {
  background: var(--flag-bg);
  border-color: var(--flag-border);
}

/* 枠と大きさは main.css の .icon-button。ここは印が付いているときの色だけ */
.flag--on {
  color: var(--flag-text);
  border-color: var(--flag-border);
}

/* ボタン類は長押しメニューを残す(✳ の「Safari で開く」を選べるように) */
.card [data-no-drag] {
  -webkit-touch-callout: default;
}

.card__memo {
  flex: 1;
  min-width: 0;
  /* 1 行目をアイコンの高さに合わせる */
  padding: 0.1875rem 0 0;
  border: none;
  background: none;
  color: var(--text);
  font: inherit;
  text-align: left;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  cursor: pointer;
}

.card__memo:hover {
  color: var(--accent);
}

/* 上に印 / コピー / ✳、下に完了ボタン */
.card__side {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  align-items: flex-end;
  gap: 0.625rem;
  flex-shrink: 0;
}

.card__tools {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.card__buttons {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.btn {
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

.btn--done:hover {
  color: var(--badge-done-text);
  border-color: var(--badge-done-text);
}

.btn--start:hover {
  color: var(--badge-inprogress-text);
  border-color: var(--badge-inprogress-text);
}

/* 着手中は状態表示を兼ねるので色を付けたままにする */
.btn--started {
  background: var(--badge-inprogress-bg);
  color: var(--badge-inprogress-text);
  border-color: var(--badge-inprogress-text);
}
</style>
