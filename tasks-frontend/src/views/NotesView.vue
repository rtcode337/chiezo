<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '@/api/client'
import { usePullToRefresh } from '@/lib/pullToRefresh'
import ErrorBanner from '@/components/ErrorBanner.vue'
import type { Note, NoteTag } from '@/api/types'

/**
 * そのほかのメモ。**タスク・プロジェクト・ルールのどれでもない**短期記憶が並ぶ
 * (決定・環境・runbook・トラブルシュート…)。溜まる量はこちらのほうが多いのに、
 * これまで画面から見る手段が無かった。
 *
 * ここは読むだけ。書くのは MCP の `remember` か本体の REST で、画面に足すと
 * 同じことをする口が 3 つになる。全文と生の項目は本体のブラウズ画面で見る。
 */
const PAGE_SIZE = 50

const notes = ref<Note[]>([])
const total = ref(0)
const tags = ref<NoteTag[]>([])
const selected = ref<string | null>(null)
const error = ref<string | null>(null)
const loading = ref(false)

const hasMore = computed(() => notes.value.length < total.value)

async function load(reset = true) {
  loading.value = true
  try {
    const offset = reset ? 0 : notes.value.length
    const page = await api.listNotes({
      tag: selected.value ?? undefined,
      limit: PAGE_SIZE,
      offset,
    })
    notes.value = reset ? page.items : [...notes.value, ...page.items]
    total.value = page.total
    error.value = null
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function loadTags() {
  try {
    tags.value = await api.noteTags()
  } catch {
    // 絞り込みの候補が出ないだけなので、一覧は見られるようにしておく
    tags.value = []
  }
}

function pick(tag: string | null) {
  selected.value = selected.value === tag ? null : tag
  void load()
}

usePullToRefresh(() => Promise.all([load(), loadTags()]))

onMounted(() => Promise.all([load(), loadTags()]))

/** 一覧では抜粋まで。全文はブラウズ画面で読む */
function excerpt(note: Note): string {
  const body = note.body.trim()
  // 1 行目は見出しとして別に出しているので、本文からは落とす
  const rest = body.startsWith(note.title) ? body.slice(note.title.length).trim() : body
  return rest.length > 160 ? `${rest.slice(0, 160)}…` : rest
}
</script>

<template>
  <section class="page">
    <div class="head">
      <h1 class="title">そのほかのメモ</h1>
      <span class="muted">{{ total }} 件</span>
    </div>

    <ErrorBanner v-if="error" :message="error" @close="error = null" />

    <p class="hint">
      タスク・プロジェクト・ルールのどれでもない短期記憶。書くのは
      <code>remember</code>(MCP)から。
    </p>

    <div v-if="tags.length" class="tags">
      <button
        v-for="t in tags"
        :key="t.tag"
        type="button"
        class="tag"
        :class="{ 'tag--on': selected === t.tag }"
        @click="pick(t.tag)"
      >
        {{ t.tag }} <span class="tag__count">{{ t.docs }}</span>
      </button>
    </div>

    <ul v-if="notes.length" class="list">
      <li v-for="note in notes" :key="note.id" class="note">
        <div class="note__head">
          <a :href="note.url" class="note__title">{{ note.title }}</a>
          <time class="note__time">{{ note.updatedAt.slice(0, 10) }}</time>
        </div>
        <p v-if="excerpt(note)" class="note__body">{{ excerpt(note) }}</p>
        <div v-if="note.tags.length" class="note__tags">
          <span v-for="t in note.tags" :key="t" class="note__tag">{{ t }}</span>
        </div>
      </li>
    </ul>
    <p v-else-if="!loading" class="muted">
      {{ selected ? 'このタグのメモはありません' : 'まだ何もありません' }}
    </p>

    <button v-if="hasMore" type="button" class="more" :disabled="loading" @click="load(false)">
      もっと見る
    </button>

    <div class="foot">
      <RouterLink to="/" class="foot__link">← トップへ</RouterLink>
    </div>
  </section>
</template>

<style scoped>
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

.hint {
  margin: 0 0 0.75rem;
  font-size: 0.75rem;
  color: var(--muted-dim);
}

.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 0.375rem;
  margin-bottom: 1rem;
}

.tag {
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--muted);
  border-radius: 999px;
  padding: 0.25rem 0.625rem;
  font-size: 0.75rem;
  cursor: pointer;
}

.tag--on {
  border-color: var(--accent);
  color: var(--accent);
}

.tag__count {
  color: var(--muted-dim);
}

.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.note {
  border: 1px solid var(--border);
  border-radius: 0.5rem;
  background: var(--surface);
  padding: 0.75rem 0.875rem;
}

.note__head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 0.75rem;
}

.note__title,
.note__title:visited {
  color: var(--text);
  font-weight: 600;
  text-decoration: none;
  overflow-wrap: anywhere;
}

.note__title:hover {
  color: var(--accent);
}

.note__time {
  flex-shrink: 0;
  font-size: 0.75rem;
  color: var(--muted-dim);
}

.note__body {
  margin: 0.375rem 0 0;
  font-size: 0.8125rem;
  color: var(--muted);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.note__tags {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem;
  margin-top: 0.5rem;
}

.note__tag {
  font-size: 0.6875rem;
  color: var(--muted-dim);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 0.0625rem 0.5rem;
}

.more {
  margin-top: 1rem;
  align-self: flex-start;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--muted);
  border-radius: 0.375rem;
  padding: 0.375rem 0.875rem;
  font-size: 0.8125rem;
  cursor: pointer;
}

.foot {
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
