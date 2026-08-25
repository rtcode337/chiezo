<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()
const open = ref(false)
const root = ref<HTMLElement | null>(null)

function toggle() {
  open.value = !open.value
}

function close() {
  open.value = false
}

function onDocPointer(event: MouseEvent) {
  if (open.value && root.value && !root.value.contains(event.target as Node)) close()
}

function onKey(event: KeyboardEvent) {
  if (event.key === 'Escape') close()
}

onMounted(() => {
  document.addEventListener('mousedown', onDocPointer)
  document.addEventListener('keydown', onKey)
})

onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocPointer)
  document.removeEventListener('keydown', onKey)
})
</script>

<template>
  <div ref="root" class="user">
    <button
      type="button"
      class="user__button"
      aria-label="アカウント"
      aria-haspopup="menu"
      :aria-expanded="open"
      @click="toggle"
    >
      <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
        <circle cx="12" cy="8" r="3.6" fill="none" stroke="currentColor" stroke-width="1.7" />
        <path
          d="M5 20c0-3.6 3.1-6 7-6s7 2.4 7 6"
          fill="none"
          stroke="currentColor"
          stroke-width="1.7"
          stroke-linecap="round"
        />
      </svg>
    </button>

    <div v-if="open" class="user__menu" role="menu">
      <p v-if="session.me?.email" class="user__email" :title="session.me.email">
        {{ session.me.email }}
      </p>
      <button type="button" class="user__logout" role="menuitem" @click="session.logout()">
        ログアウト
      </button>
    </div>
  </div>
</template>

<style scoped>
.user {
  position: relative;
  display: inline-flex;
}

.user__button {
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

.user__button:hover {
  color: var(--text);
  border-color: var(--accent);
}

.user__menu {
  position: absolute;
  top: calc(100% + 0.4rem);
  right: 0;
  z-index: 20;
  min-width: 11rem;
  max-width: 16rem;
  padding: 0.5rem;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface-raised);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
}

.user__email {
  margin: 0 0 0.375rem;
  padding: 0 0.375rem;
  font-size: 0.75rem;
  color: var(--muted-dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.user__logout {
  display: block;
  width: 100%;
  padding: 0.5rem 0.5rem;
  border: none;
  border-radius: 8px;
  background: none;
  color: var(--text);
  font-size: 0.875rem;
  font-family: inherit;
  text-align: left;
  cursor: pointer;
}

.user__logout:hover {
  background: var(--surface);
  color: var(--danger);
}
</style>
