<script setup lang="ts">
import { ref } from 'vue'

const props = withDefaults(
  defineProps<{ text: string; label?: string; icon?: boolean }>(),
  { icon: false },
)

const copied = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined

async function copy() {
  try {
    await navigator.clipboard.writeText(props.text)
  } catch {
    // クリップボード API が使えない環境向けのフォールバック
    const area = document.createElement('textarea')
    area.value = props.text
    area.style.position = 'fixed'
    area.style.opacity = '0'
    document.body.appendChild(area)
    area.select()
    try {
      document.execCommand('copy')
    } finally {
      document.body.removeChild(area)
    }
  }
  copied.value = true
  clearTimeout(timer)
  timer = setTimeout(() => (copied.value = false), 1200)
}
</script>

<template>
  <!-- アイコン版: クリップボードに貼り付ける形。複製ボタンに見えないようにする -->
  <button
    v-if="icon"
    type="button"
    class="icon-button"
    :class="{ 'icon-button--done': copied }"
    :aria-label="label ?? 'クリップボードにコピー'"
    :title="copied ? 'コピーしました' : (label ?? 'クリップボードにコピー')"
    @click.stop.prevent="copy"
  >
    <svg v-if="copied" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
      <path
        d="M5 12.5l4 4 10-10"
        fill="none"
        stroke="currentColor"
        stroke-width="2.2"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
    </svg>
    <svg v-else viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
      <rect x="8" y="4" width="11" height="14" rx="2" fill="none" stroke="currentColor" stroke-width="1.7" />
      <path
        d="M6 8v10a2 2 0 0 0 2 2h7"
        fill="none"
        stroke="currentColor"
        stroke-width="1.7"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
    </svg>
  </button>

  <button
    v-else
    type="button"
    class="text"
    :class="{ 'text--done': copied }"
    @click.stop.prevent="copy"
  >
    {{ copied ? 'コピーしました' : (label ?? 'コピー') }}
  </button>
</template>

<style scoped>
/* 枠と大きさは main.css の .icon-button(書き出し / 読み込みと共通)。ここは済み表示だけ */
.icon-button--done {
  color: var(--badge-done-text);
  border-color: var(--badge-done-text);
}

.text {
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

.text:hover {
  color: var(--text);
  border-color: var(--accent);
}

.text--done {
  color: var(--accent);
  border-color: var(--accent);
}
</style>
