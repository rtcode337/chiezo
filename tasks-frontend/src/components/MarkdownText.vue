<script setup lang="ts">
import { computed } from 'vue'
import { renderMarkdown } from '@/lib/markdown'

const props = defineProps<{ source?: string | null }>()

// renderMarkdown は HTML をエスケープしてから既知の記法だけタグに戻すため、
// ここでの v-html は入力由来のタグを通さない
const html = computed(() => (props.source ? renderMarkdown(props.source) : ''))
</script>

<template>
  <div v-if="html" class="markdown" v-html="html" />
</template>

<style scoped>
.markdown :deep(> *:first-child) {
  margin-top: 0;
}

.markdown :deep(> *:last-child) {
  margin-bottom: 0;
}

.markdown :deep(p) {
  margin: 0.5rem 0;
  line-height: 1.7;
}

.markdown :deep(h2),
.markdown :deep(h3),
.markdown :deep(h4),
.markdown :deep(h5) {
  margin: 1rem 0 0.5rem;
  font-size: 1rem;
}

.markdown :deep(ul),
.markdown :deep(ol) {
  margin: 0.5rem 0;
  padding-left: 1.25rem;
  line-height: 1.7;
}

.markdown :deep(li.task) {
  list-style: none;
  margin-left: -1.25rem;
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
}

.markdown :deep(li.task .done) {
  color: var(--muted-dim);
  text-decoration: line-through;
}

.markdown :deep(code) {
  background: var(--code-bg);
  padding: 0.0625rem 0.25rem;
  border-radius: 4px;
  font-size: 0.875em;
}

.markdown :deep(pre) {
  background: var(--code-bg);
  padding: 0.75rem;
  border-radius: 8px;
  overflow-x: auto;
}

.markdown :deep(pre code) {
  background: none;
  padding: 0;
}

.markdown :deep(blockquote) {
  margin: 0.5rem 0;
  padding-left: 0.75rem;
  border-left: 3px solid var(--border);
  color: var(--muted);
}

.markdown :deep(a) {
  color: var(--accent);
}

.markdown :deep(hr) {
  border: none;
  border-top: 1px solid var(--border);
  margin: 1rem 0;
}
</style>
