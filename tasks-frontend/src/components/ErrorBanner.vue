<script setup lang="ts">
withDefaults(defineProps<{ message: string; kind?: 'error' | 'warn' }>(), { kind: 'error' })
</script>

<template>
  <p class="banner" :class="`banner--${kind}`" role="alert">{{ message }}</p>
</template>

<style scoped>
.banner {
  margin: 0;
  padding: 0.625rem 1rem;
  font-size: 0.875rem;
  text-align: center;
}

.banner--error {
  background: var(--danger-bg);
  color: var(--danger);
}

.banner--warn {
  background: var(--warn-bg);
  color: var(--warn);
}
</style>
