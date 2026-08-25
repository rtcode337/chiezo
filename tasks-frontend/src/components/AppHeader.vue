<script setup lang="ts">
import { RouterLink } from 'vue-router'
import ThemeToggle from '@/components/ThemeToggle.vue'
import UserMenu from '@/components/UserMenu.vue'
</script>

<template>
  <header class="header">
    <div class="header__inner">
      <RouterLink to="/" class="header__brand">やること</RouterLink>
      <nav class="header__nav">
        <RouterLink to="/" class="header__link" active-class="header__link--active" exact-active-class="header__link--active">トップ</RouterLink>
        <RouterLink to="/rules" class="header__link" active-class="header__link--active">ルール</RouterLink>
        <ThemeToggle />
        <UserMenu />
      </nav>
    </div>
  </header>
</template>

<style scoped>
.header {
  position: sticky;
  top: 0;
  z-index: 10;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding-top: env(safe-area-inset-top);
}

.header__inner {
  /* 本文と同じ外枠。ずらすとロゴと一覧の左端が揃わない */
  max-width: var(--content-max);
  margin: 0 auto;
  padding: 0.625rem 1rem;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  flex-wrap: wrap;
}

.header__brand {
  font-weight: 700;
  letter-spacing: 0.02em;
  color: var(--text);
  text-decoration: none;
}

.header__nav {
  display: flex;
  align-items: center;
  gap: 0.875rem;
}

.header__link {
  color: var(--muted);
  text-decoration: none;
  font-size: 0.875rem;
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
  font-family: inherit;
}

.header__link:hover {
  color: var(--text);
}

.header__link--active {
  color: var(--accent);
  font-weight: 600;
}

.header__link--button {
  font-size: 0.875rem;
}
</style>
