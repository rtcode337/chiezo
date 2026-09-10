<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { api } from '@/api/client'
import { usePullToRefresh } from '@/lib/pullToRefresh'
import ErrorBanner from '@/components/ErrorBanner.vue'
import type { MediaGroup, MediaJob } from '@/api/types'

/**
 * 生成物の見比べ —— 1 組の中身。
 *
 * ここで初めて絵と音を読み込む（一覧は見出しだけ）。**音は AI 自身が聴けない**ので、
 * 聴き比べて選ぶ場所はここしかない。選んだ結果は記録に残り、頼んだ AI が
 * `media_picks` で引きに来る。
 */
const route = useRoute()
const group = ref<MediaGroup | null>(null)
const error = ref<string | null>(null)
const loading = ref(false)
const noteFor = ref<Record<string, string>>({})
/** 文章の中身。**開いたときに取りに行く** —— job には本文を持たせていない
 *  （一覧を引くたびに何万字も運ばないため、置き場のファイルとして持っている）。 */
const bodyFor = ref<Record<string, string>>({})
/** 全文を出しているか。長い文章は畳んでおく（端末では 4 万字がそのまま並ぶと読めない）。 */
const openFor = ref<Record<string, boolean>>({})

/** 畳んでいるときに見せる長さ。ここまでで「読む価値があるか」は判断できる。 */
const PREVIEW_CHARS = 600

const key = computed(() => String(route.params.key ?? ''))

async function load() {
  loading.value = true
  try {
    group.value = await api.getMediaGroup(key.value)
    error.value = null
    await loadBodies()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '読み込めませんでした'
  } finally {
    loading.value = false
  }
}

/**
 * 文章の本文を取ってくる。**絵や音と違って `<img>` / `<audio>` が勝手に取ってくれない**
 * ので、ここで読みに行く。取れなかった案は飛ばす（1 本の失敗で組ごと出せなくしない）。
 */
async function loadBodies() {
  const jobs = (group.value?.jobs ?? []).filter((j) => j.kind === 'text')
  await Promise.all(
    jobs.map(async (job) => {
      const url = job.files[0]?.url
      if (!url || bodyFor.value[job.id] !== undefined) return
      try {
        const res = await fetch(url)
        bodyFor.value[job.id] = res.ok ? await res.text() : '（本文を読めませんでした）'
      } catch {
        bodyFor.value[job.id] = '（本文を読めませんでした）'
      }
    }),
  )
}

function shown(job: MediaJob): string {
  const text = bodyFor.value[job.id] ?? ''
  if (openFor.value[job.id] || text.length <= PREVIEW_CHARS) return text
  return text.slice(0, PREVIEW_CHARS) + '…'
}

function longEnough(job: MediaJob): boolean {
  return (bodyFor.value[job.id] ?? '').length > PREVIEW_CHARS
}

/** 文字数。**一覧で長さが読める**ように job の `seconds` に入れてある。 */
function chars(job: MediaJob): string {
  const n = job.seconds ?? (bodyFor.value[job.id] ?? '').length
  return n ? `${Math.round(n).toLocaleString('ja-JP')} 字` : ''
}

async function pick(job: MediaJob) {
  try {
    await api.pickMedia(job.id, noteFor.value[job.id] ?? '')
    await load()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '採用できませんでした'
  }
}

async function unpick(job: MediaJob) {
  try {
    await api.unpickMedia(job.id)
    await load()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '取り消せませんでした'
  }
}

/** 種類の呼び名。**そのまま出さない** —— 画面に英語の識別子が出ると、
 *  何の組なのかを読む人が推測することになる。 */
function kindLabel(kind: string): string {
  return { audio: '音', image: '画像', video: '動画', speech: '読み上げ', text: '文章' }[kind] ?? kind
}

/** 案の見出し。組の中の何番目かは並び順で決まる（頼んだ順）。 */
function label(index: number): string {
  return `案 ${index + 1}`
}

function when(iso: string): string {
  // **見せるのは日本時間**。保存は UTC のまま
  return new Date(iso).toLocaleString('ja-JP', { timeZone: 'Asia/Tokyo' })
}

usePullToRefresh(load)
onMounted(load)
watch(key, load)
</script>

<template>
  <section class="group">
    <header class="group__head">
      <RouterLink to="/media" class="group__back">← 見比べ</RouterLink>
      <button type="button" class="btn" :disabled="loading" @click="load">読み直す</button>
    </header>

    <ErrorBanner v-if="error" :message="error" />

    <template v-if="group">
      <h1 class="group__title">{{ group.title }}</h1>
      <p class="group__meta">
        {{ kindLabel(group.kind) }}
        ・ {{ group.count }} 案 ・ {{ when(group.created_at) }}
      </p>

      <div class="items">
        <div
          v-for="(job, index) in group.jobs"
          :key="job.id"
          class="item"
          :class="{ 'item--picked': job.picked_at }"
        >
          <div class="item__head">
            <strong>{{ label(index) }}</strong>
            <span class="item__by">{{ job.backend }}<template v-if="job.model"> / {{ job.model }}</template></span>
          </div>

          <p v-if="job.state !== 'done'" class="item__state">
            {{ job.state === 'failed' ? '失敗' : '作成中' }}
            <span v-if="job.error" class="item__error">{{ job.error }}</span>
          </p>

          <template v-if="job.kind === 'text'">
            <p v-if="chars(job)" class="item__chars">{{ chars(job) }}</p>
            <pre class="item__text">{{ shown(job) }}</pre>
            <button
              v-if="longEnough(job)"
              type="button"
              class="btn btn--quiet"
              @click="openFor[job.id] = !openFor[job.id]"
            >
              {{ openFor[job.id] ? '畳む' : '全文を読む' }}
            </button>
          </template>
          <template v-else v-for="file in job.files" :key="file.url">
            <img v-if="job.kind === 'image'" :src="file.url" :alt="label(index)" class="item__image" />
            <audio v-else :src="file.url" controls preload="none" class="item__audio" />
          </template>

          <details class="item__prompt">
            <summary>依頼文</summary>
            <p>{{ job.prompt }}</p>
          </details>

          <div class="item__pick">
            <template v-if="job.picked_at">
              <span class="item__badge">採用</span>
              <span v-if="job.picked_note" class="item__note">{{ job.picked_note }}</span>
              <button type="button" class="btn btn--quiet" @click="unpick(job)">取り消す</button>
            </template>
            <template v-else-if="job.state === 'done'">
              <input
                v-model="noteFor[job.id]"
                class="item__input"
                type="text"
                placeholder="一言（任意）"
              />
              <button type="button" class="btn" @click="pick(job)">これにする</button>
            </template>
          </div>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.group {
  display: grid;
  gap: 12px;
}

.group__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.group__back {
  color: var(--muted);
  text-decoration: none;
}

.group__title {
  margin: 0;
  font-size: 1.2rem;
}

.group__meta {
  margin: 0;
  color: var(--muted);
  font-size: 0.85rem;
}

.items {
  display: grid;
  gap: 12px;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
}

.item {
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 10px;
  display: grid;
  gap: 8px;
  align-content: start;
}

/* 採用したものは一目で分かるようにする（組から選ぶのは 1 つ） */
.item--picked {
  border-color: var(--accent, #6a9);
  box-shadow: 0 0 0 2px var(--accent, #6a9) inset;
}

.item__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}

.item__by,
.item__state {
  color: var(--muted);
  font-size: 0.8rem;
}

.item__error {
  display: block;
}

.item__image {
  width: 100%;
  height: auto;
  border-radius: 6px;
  background: var(--line);
}

.item__audio {
  width: 100%;
}

.item__prompt summary {
  cursor: pointer;
  color: var(--muted);
  font-size: 0.85rem;
}

.item__prompt p {
  margin: 6px 0 0;
  font-size: 0.85rem;
  white-space: pre-wrap;
  word-break: break-word;
}

.item__pick {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.item__input {
  flex: 1 1 100px;
  min-width: 0;
}

.item__badge {
  font-weight: 700;
  color: var(--accent, #6a9);
}

.item__note {
  font-size: 0.85rem;
  color: var(--muted);
}
/* 文章の案。**等幅にしない** —— 読み物なので、長く読める字面のほうがよい。
   改行はそのまま出す（Markdown として組み直すと、見比べの邪魔になる装飾が入る）。 */
.item__text {
  margin: 0;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--panel, #fff);
  font-family: inherit;
  font-size: 0.92rem;
  line-height: 1.9;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 70vh;
  overflow-y: auto;
}

.item__chars {
  margin: 0;
  color: var(--muted);
  font-size: 0.8rem;
}
</style>
