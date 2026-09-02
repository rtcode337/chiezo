<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '@/api/client'
import { usePullToRefresh } from '@/lib/pullToRefresh'
import ErrorBanner from '@/components/ErrorBanner.vue'
import type { MediaGroupSummary } from '@/api/types'

/**
 * 生成物の見比べ —— 依頼の一覧。
 *
 * **一覧では絵も音も出さない。** 依頼が溜まるほど、開くつもりのないものまで
 * 毎回読み込むことになる（音は 1 本で数 MB ある）。ここに出すのは見出し・依頼日時・
 * 種類・件数までで、中身は開いた先（`/media/:key`）で見る。
 */
const groups = ref<MediaGroupSummary[]>([])
const error = ref<string | null>(null)
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const page = await api.listMediaGroups(50)
    groups.value = page.groups
    error.value = null
  } catch (e) {
    error.value = e instanceof Error ? e.message : '読み込めませんでした'
  } finally {
    loading.value = false
  }
}

function when(iso: string): string {
  // **見せるのは日本時間**。保存は UTC のまま
  return new Date(iso).toLocaleString('ja-JP', { timeZone: 'Asia/Tokyo' })
}

function kindLabel(kind: string): string {
  return { image: '画像', audio: '音', video: '動画', speech: '読み上げ' }[kind] ?? kind
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

    <ul v-else class="list">
      <li v-for="group in groups" :key="group.key" class="list__item">
        <RouterLink :to="`/media/${encodeURIComponent(group.key)}`" class="row">
          <span class="row__kind">{{ kindLabel(group.kind) }}</span>
          <span class="row__title">{{ group.title }}</span>
          <span class="row__meta">
            <span v-if="group.picked" class="row__picked">採用済み</span>
            {{ group.count }} 案 ・ {{ when(group.created_at) }}
          </span>
        </RouterLink>
      </li>
    </ul>
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

.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 8px;
}

.row {
  display: grid;
  grid-template-columns: auto 1fr auto;
  align-items: baseline;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  text-decoration: none;
  color: inherit;
}

.row:hover {
  border-color: var(--accent, #6a9);
}

.row__kind {
  font-size: 0.75rem;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--line);
  color: var(--muted);
  white-space: nowrap;
}

.row__title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.row__meta {
  font-size: 0.8rem;
  color: var(--muted);
  white-space: nowrap;
}

.row__picked {
  color: var(--accent, #6a9);
  font-weight: 700;
  margin-right: 6px;
}

/* 狭い画面では日時を下の行へ回す（見出しを削らない） */
@media (max-width: 40rem) {
  .row {
    grid-template-columns: auto 1fr;
  }

  .row__meta {
    grid-column: 1 / -1;
  }
}
</style>
