<script setup lang="ts">
import { useSessionStore } from '@/stores/session'
import ThemeToggle from '@/components/ThemeToggle.vue'

const session = useSessionStore()
</script>

<template>
  <div class="login">
    <div class="login__toolbar">
      <ThemeToggle />
    </div>
    <h1 class="login__title">やること</h1>
    <p class="login__lead">Claude Code に依頼したいタスクの待ち行列</p>
    <button type="button" class="login__button" @click="session.login()">Google でログイン</button>
  </div>
</template>

<style scoped>
.login {
  position: relative;
  min-height: 70dvh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.75rem;
  text-align: center;
}

.login__toolbar {
  position: absolute;
  top: 1rem;
  right: 0;
}

.login__title {
  margin: 0;
  font-size: 1.75rem;
  letter-spacing: 0.02em;
}

.login__lead {
  margin: 0 0 1.5rem;
  color: var(--muted);
  font-size: 0.9375rem;
}

.login__button {
  padding: 0.75rem 1.5rem;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text);
  font-size: 1rem;
  cursor: pointer;
}

.login__button:hover {
  border-color: var(--accent);
}
</style>
