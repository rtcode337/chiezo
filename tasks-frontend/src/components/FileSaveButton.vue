<script setup lang="ts">
import { saveTextFile } from '@/lib/fileTransfer'

/**
 * 表示中の本文をそのままファイルに保存する。本文の枠のすぐ外、右上に置く。
 * **保存する文字列はコピーと同じもの**なので、どちらで持ち出しても読み込みに渡せる。
 *
 * アイコンではなく**文言のまま**出す —— 下向き矢印だけでは「ファイルに保存」なのか
 * 「画面内で何かが落ちてくる」のか読み手に分からないため。
 */
const props = withDefaults(
  defineProps<{ text: string; filename: string; mime?: string; label?: string }>(),
  { mime: 'text/plain', label: 'ファイルに書き出す' },
)
</script>

<template>
  <button
    type="button"
    class="text-button"
    @click.stop.prevent="saveTextFile(props.filename, props.text, props.mime)"
  >
    {{ label }}
  </button>
</template>
