<script setup lang="ts">
import { computed } from 'vue'
import { useThemeStore } from '@/stores/theme'

const theme = useThemeStore()
const isDark = computed(() => theme.theme === 'dark')
const label = computed(() => (isDark.value ? '明るいテーマに切り替え' : '暗いテーマに切り替え'))
</script>

<template>
  <button type="button" class="toggle" :aria-label="label" :title="label" @click="theme.toggle()">
    <!-- 明るいときは月(暗くする)、暗いときは太陽(明るくする)を出す -->
    <svg
      v-if="isDark"
      class="toggle__icon"
      viewBox="0 0 24 24"
      width="20"
      height="20"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="4.2" fill="currentColor" />
      <g stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
        <line x1="12" y1="2.5" x2="12" y2="5" />
        <line x1="12" y1="19" x2="12" y2="21.5" />
        <line x1="2.5" y1="12" x2="5" y2="12" />
        <line x1="19" y1="12" x2="21.5" y2="12" />
        <line x1="5.2" y1="5.2" x2="7" y2="7" />
        <line x1="17" y1="17" x2="18.8" y2="18.8" />
        <line x1="18.8" y1="5.2" x2="17" y2="7" />
        <line x1="7" y1="17" x2="5.2" y2="18.8" />
      </g>
    </svg>
    <svg v-else class="toggle__icon" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
      <path
        d="M20 14.2A8 8 0 1 1 9.8 4a6.4 6.4 0 0 0 10.2 10.2z"
        fill="currentColor"
      />
    </svg>
  </button>
</template>

<style scoped>
.toggle {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 2rem;
  height: 2rem;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  color: var(--muted);
  cursor: pointer;
}

.toggle:hover {
  color: var(--text);
  border-color: var(--accent);
}

.toggle__icon {
  display: block;
}
</style>
