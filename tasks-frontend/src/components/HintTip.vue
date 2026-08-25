<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'

/**
 * 見出しの右に置く「?」。押すと説明をチップで出す。
 *
 * **説明を常に出しておくと、毎回読む必要のない文章が本文の場所を取る** ——
 * 一度読めば分かる決まりごと(何が含まれるか・重複をどう扱うか)は、
 * 必要なときだけ開けるようにする。開閉は押すたびに切り替わる(閉じるのも同じ「?」)。
 *
 * **チップは `position: fixed` で、開くたびに置き場所を計算する。** 「?」からの相対位置
 * (`absolute`)に置くと、見出しが画面の右寄りにあるときや画面が狭いときにチップの右側が
 * 画面外へ出てしまう —— CSS だけでは「はみ出したときだけ寄せる」が書けないため、
 * ボタンの位置を測って画面内に収める。
 */
const open = ref(false)
const toggle = ref<HTMLButtonElement | null>(null)
const position = ref<{ top: string; left: string; width: string }>()

/** 画面端との最小の隙間 */
const MARGIN = 8
/** チップの幅の上限(これ以上広げると 1 行が長くなって読みづらい) */
const MAX_WIDTH = 320

function place() {
  const button = toggle.value
  if (!button) return
  const rect = button.getBoundingClientRect()
  const width = Math.min(MAX_WIDTH, window.innerWidth - MARGIN * 2)
  // 「?」の少し左を基準にしつつ、両端で画面からはみ出さないところまで寄せる
  const left = Math.min(Math.max(rect.left - MARGIN, MARGIN), window.innerWidth - width - MARGIN)
  position.value = { top: `${rect.bottom + 6}px`, left: `${left}px`, width: `${width}px` }
}

function hide() {
  open.value = false
}

function onToggle() {
  if (open.value) return hide()
  place()
  open.value = true
  // 画面の大きさが変わると測った位置がずれるので、開いたままにせず閉じる
  window.addEventListener('resize', hide, { once: true })
}

onBeforeUnmount(() => window.removeEventListener('resize', hide))
</script>

<template>
  <span class="tip">
    <button
      ref="toggle"
      type="button"
      class="tip__toggle"
      :class="{ 'tip__toggle--on': open }"
      :aria-expanded="open"
      aria-label="説明を見る"
      title="説明を見る"
      @click.stop.prevent="onToggle"
    >
      ?
    </button>
    <span v-if="open" class="tip__body" role="tooltip" :style="position">
      <slot />
    </span>
  </span>
</template>

<style scoped>
.tip {
  display: inline-flex;
  vertical-align: middle;
}

.tip__toggle {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.125rem;
  height: 1.125rem;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: 50%;
  background: var(--surface);
  color: var(--muted);
  font-size: 0.6875rem;
  font-family: inherit;
  font-weight: 600;
  line-height: 1;
  cursor: pointer;
}

.tip__toggle:hover,
.tip__toggle--on {
  color: var(--accent);
  border-color: var(--accent);
}

/* 位置と幅は place() が付ける(画面内に収めるため)。ここは見た目だけ */
.tip__body {
  position: fixed;
  z-index: 30;
  padding: 0.5rem 0.625rem;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.18);
  font-size: 0.75rem;
  font-weight: 400;
  line-height: 1.6;
  color: var(--muted);
  white-space: normal;
}
</style>
