<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { useSessionStore } from '@/stores/session'
import { OfflineError } from '@/api/client'
import { inFlightRequests, onRequestFailure, onUnauthorized } from '@/lib/network'
import { currentRefreshHandler } from '@/lib/pullToRefresh'
import { isDragActive } from '@/lib/dragSort'
import ErrorBanner from '@/components/ErrorBanner.vue'
import AppHeader from '@/components/AppHeader.vue'

const session = useSessionStore()
const router = useRouter()
const route = useRoute()
const bootError = ref<string | null>(null)
const offline = ref(!navigator.onLine)

// ビルド番号(vite.config.ts の define で注入)。フッターに出す
const buildNumber = __BUILD_NUMBER__

// ---- 通信中オーバーレイ ----
// 進行中のリクエストがあるあいだ画面全体を覆う。ただし表示は 0.3 秒待ち、
// すぐ終わる通信では出さない(保存のたびに画面が白く点滅するのを防ぐ)
const BUSY_DELAY_MS = 300
const busy = ref(false)
let busyTimer: number | undefined
watch(
  () => inFlightRequests.value > 0,
  (active) => {
    window.clearTimeout(busyTimer)
    if (active) {
      busyTimer = window.setTimeout(() => (busy.value = true), BUSY_DELAY_MS)
    } else {
      busy.value = false
    }
  },
)

// ---- 通信失敗 ----
// リクエストが届かない・応答が返らないときはダイアログを 1 回出す。**画面は動かさない**
// —— 認証の問題ではないので、ログイン画面へ送っても直らないし、やりかけの操作を見失う
// (埋め込みの面ではそもそも戻る先が無く、押しても 404 になるログインボタンが出ていた)。
// 並行するリクエストが同時に失敗しても、ダイアログを続けざまに出さない
const FAILURE_QUIET_MS = 1000
let failureNotified = false
onRequestFailure(() => {
  if (failureNotified) return
  failureNotified = true
  window.alert('サーバーとの通信に失敗しました。')
  window.setTimeout(() => (failureNotified = false), FAILURE_QUIET_MS)
})

// ---- 未認証 ----
// 認証が切れた(401)ときだけログイン画面へ送る。ここは送れば直る失敗なので誘導してよい。
// 埋め込みの面は認証を持たないので、この経路には来ない
onUnauthorized(() => {
  if (route.name === 'login') return
  session.reset()
  void router.replace({ name: 'login' })
})

// 認証確認(/api/me)が済み、ログイン済みと分かるまで画面を描画しない。
// 未認証のまま描いてよいのはログイン画面だけ
const canRender = computed(() => session.checked && (session.me !== null || route.name === 'login'))

// ---- 下に引っ張って更新(PWA にはリロード手段が無いため) ----
// 指の移動量には減衰(DAMP)を掛けてから pull に入れる。TRIGGER は減衰後の値
const DAMP = 0.4
const TRIGGER = 32 // 指の移動 80px 相当
const PULL_MAX = 80

/** 広い画面では本文の側がスクロールする(下に引っ張って更新の判定に要る) */
const mainEl = ref<HTMLElement | null>(null)

const pull = ref(0)
const refreshing = ref(false)
const armed = computed(() => pull.value >= TRIGGER)
let startY = 0
let tracking = false

function onTouchStart(e: TouchEvent) {
  // 最上部で触れたときだけ追跡を始める(スクロール中は何もしない)。
  // 広い画面では縦に流れるのは <main> の側なので、そちらの位置も見る
  // (window は動かないので scrollY だけだと常に「最上部」になってしまう)
  const scrolled = window.scrollY > 0 || (mainEl.value?.scrollTop ?? 0) > 0
  tracking = !scrolled && !refreshing.value
  startY = e.touches[0].clientY
}

function onTouchMove(e: TouchEvent) {
  if (!tracking) return
  // 長押しからの並び替え中は指を下に動かすので、引っ張り更新と食い合う。並び替えを優先する
  if (isDragActive()) {
    tracking = false
    pull.value = 0
    return
  }
  if (window.scrollY > 0 || (mainEl.value?.scrollTop ?? 0) > 0) {
    tracking = false
    pull.value = 0
    return
  }
  const dy = e.touches[0].clientY - startY
  pull.value = dy > 0 ? Math.min(dy * DAMP, PULL_MAX) : 0
}

async function onTouchEnd() {
  if (!tracking) return
  tracking = false
  if (!armed.value) {
    pull.value = 0
    return
  }
  refreshing.value = true
  try {
    const handler = currentRefreshHandler()
    if (handler) {
      await handler()
    } else {
      // 再読込処理を持たない画面はページ全体をリロード
      window.location.reload()
      return
    }
  } finally {
    refreshing.value = false
    pull.value = 0
  }
}

function onTouchCancel() {
  tracking = false
  pull.value = 0
}

onMounted(async () => {
  window.addEventListener('online', () => (offline.value = false))
  window.addEventListener('offline', () => (offline.value = true))
  try {
    const ok = await session.load()
    if (!ok) {
      await router.replace({ name: 'login' })
    } else if (route.name === 'login') {
      await router.replace({ name: 'home' })
    }
  } catch (error) {
    // 通信失敗は onRequestFailure 側でダイアログを出しているので、ここでは出さない
    if (!(error instanceof OfflineError)) {
      bootError.value = error instanceof Error ? error.message : String(error)
    }
  }
})
</script>

<template>
  <div
    class="app"
    @touchstart.passive="onTouchStart"
    @touchmove.passive="onTouchMove"
    @touchend.passive="onTouchEnd"
    @touchcancel.passive="onTouchCancel"
  >
    <!-- 引っ張り量に応じて出るインジケータ -->
    <div
      v-if="pull > 0 || refreshing"
      class="ptr"
      :style="refreshing ? undefined : { opacity: String(Math.min(pull / TRIGGER, 1)) }"
      aria-hidden="true"
    >
      <span v-if="refreshing" class="ptr__spinner" />
      <span v-else class="ptr__arrow" :class="{ 'ptr__arrow--armed': armed }">↓</span>
    </div>

    <AppHeader v-if="session.me" />
    <!-- オフライン時はバナーのみ。入力の退避は将来検討 (仕様書 §8) -->
    <ErrorBanner v-if="offline" kind="warn" message="オフラインです。通信が回復するまで保存できません。" />
    <ErrorBanner v-if="bootError" :message="bootError" />
    <main ref="mainEl" class="app__main">
      <RouterView v-if="canRender" />
      <p v-else class="app__loading">読み込み中…</p>
    </main>

    <!-- ビルド番号。SW の旧キャッシュや未更新のイメージを見ていないかをここで見分ける -->
    <footer class="app__footer">ビルド {{ buildNumber }}</footer>

    <!-- 通信中は半透明の白で画面全体を覆う(0.3 秒以上かかる通信のみ。操作もブロックする) -->
    <div v-if="busy" class="busy" role="status" aria-label="通信中">
      <span class="busy__spinner" aria-hidden="true" />
      <span class="busy__label">通信中…</span>
    </div>
  </div>
</template>

<style scoped>
.app {
  min-height: 100dvh;
  display: flex;
  flex-direction: column;
}

.app__main {
  flex: 1;
  width: 100%;
  max-width: var(--content-max);
  margin: 0 auto;
  /* 下端の safe-area はフッター側で確保するので、ここは本文とフッターの間の余白だけ */
  padding: 0 1rem 4rem;
}

.app__loading {
  padding: 2rem 0;
  color: var(--muted);
}

/* 広い画面では **アプリ全体を画面 1 枚に収め**、本文の側をスクロールさせる。
   一覧の段組み(ProjectGroups)が「画面の下端まで来たら右の段へ折り返す」ためには
   一覧に確定した高さが要り、その高さをここの余りから渡す。
   スマホ(段組みをしない幅)は従来どおりページ全体が縦に伸びる */
@media (min-width: 64rem) {
  .app {
    height: 100dvh;
  }

  .app__main {
    /* flex アイテムは既定で縮まないので、min-height: 0 が無いと余りを渡せない */
    min-height: 0;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    /* 下端の余白はフッターが持つ。段の高さを削らないよう詰める */
    padding-bottom: 1rem;
  }
}

/* フッター: 横線の下にビルド番号。本文と同じ幅で線を揃える */
.app__footer {
  width: 100%;
  max-width: var(--content-max);
  margin: 0 auto;
  padding: 0.5rem 1rem calc(0.75rem + env(safe-area-inset-bottom));
  border-top: 1px solid var(--border);
  color: var(--muted-dim);
  font-size: 0.6875rem;
  text-align: center;
}

/* 覆いは白で固定なので、上に載せる色もテーマ変数ではなく明テーマ相当の固定値を使う
   (暗テーマの文字色は白に近く、白い覆いの上では見えなくなるため) */
.busy {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.625rem;
  background: rgba(255, 255, 255, 0.6);
}

.busy__spinner {
  width: 1.75rem;
  height: 1.75rem;
  border: 3px solid rgba(0, 0, 0, 0.15);
  border-top-color: #1478c8;
  border-radius: 50%;
  animation: ptr-spin 0.7s linear infinite;
}

.busy__label {
  color: #3a4654;
  font-size: 0.875rem;
}

.ptr {
  position: fixed;
  top: calc(3.25rem + env(safe-area-inset-top));
  left: 50%;
  transform: translateX(-50%);
  z-index: 30;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 2.25rem;
  height: 2.25rem;
  border: 1px solid var(--border);
  border-radius: 50%;
  background: var(--surface-raised);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.12);
  pointer-events: none;
}

.ptr__arrow {
  font-size: 1rem;
  color: var(--muted);
  transition: transform 0.15s ease;
}

/* しきい値を超えたら矢印を反転して「離すと更新」を示す */
.ptr__arrow--armed {
  transform: rotate(180deg);
  color: var(--accent);
}

.ptr__spinner {
  width: 1rem;
  height: 1rem;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: ptr-spin 0.7s linear infinite;
}

@keyframes ptr-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
