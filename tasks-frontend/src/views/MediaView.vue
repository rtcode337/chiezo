<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '@/api/client'
import { usePullToRefresh } from '@/lib/pullToRefresh'
import ErrorBanner from '@/components/ErrorBanner.vue'
import type { MediaGroup, MediaJob } from '@/api/types'

/**
 * 生成物の見比べ。**何案か作らせたものを、依頼文つきで並べて選ぶ画面。**
 *
 * なぜ要るか —— AI は自分が作らせた絵を見られるが、**音は聴けない**し、
 * リモートで繋いでいる人は手元のファイルも開けない。結果、
 * 「4 案作ったので聴いてください」と言われても確かめる場所が無かった。
 *
 * ここは読むのと印を付けるだけ。**生成は頼めない** —— あちらは課金が走るので、
 * 外に出すこの面には載せていない（`app/tasks_app.py` の冒頭）。
 * 選んだ結果は記録に残り、頼んだ AI が `media_picks` で引きに来る。
 */
const groups = ref<MediaGroup[]>([])
const error = ref<string | null>(null)
const loading = ref(false)
const noteFor = ref<Record<string, string>>({})

async function load() {
  loading.value = true
  try {
    const page = await api.listMediaGroups(30)
    groups.value = page.groups
    error.value = null
  } catch (e) {
    error.value = e instanceof Error ? e.message : '読み込めませんでした'
  } finally {
    loading.value = false
  }
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
</script>

<template>
  <section class="media">
    <header class="media__head">
      <h1>見比べ</h1>
      <button type="button" class="btn" :disabled="loading" @click="load">読み直す</button>
    </header>

    <ErrorBanner v-if="error" :message="error" />

    <p v-if="!loading && groups.length === 0" class="media__empty">
      まだ何も作られていません。AI に絵や音を頼むと、ここに並びます。
    </p>

    <article v-for="group in groups" :key="group.group || group.jobs[0].id" class="group">
      <header class="group__head">
        <h2>{{ group.group || '（名前なし）' }}</h2>
        <span class="group__meta">{{ group.kind === 'audio' ? '音' : '絵' }} ・ {{ when(group.created_at) }}</span>
      </header>

      <div class="group__items">
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

          <template v-for="file in job.files" :key="file.url">
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
    </article>
  </section>
</template>

<style scoped>
.media {
  display: grid;
  gap: 20px;
}

.media__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.media__empty {
  color: var(--muted);
}

.group {
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 14px;
  display: grid;
  gap: 12px;
}

.group__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.group__head h2 {
  font-size: 1.05rem;
  margin: 0;
}

.group__meta {
  color: var(--muted);
  font-size: 0.85rem;
}

.group__items {
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
</style>
