<script setup lang="ts">
import { pickTextFile } from '@/lib/fileTransfer'

/**
 * ファイルを選ばせて中身を返す。入力欄の見出しと同じ列(右端)に置く。
 * **入力欄に流し込むだけ**で取り込みは走らせない —— 何が入るかを確認してから
 * 実行させる流れ(dryRun)は、貼り付けたときと同じにする。
 *
 * アイコンではなく**文言のまま**出す(書き出し側と同じ理由)。
 */
const props = withDefaults(defineProps<{ accept: string; label?: string }>(), {
  label: 'ファイルから読み込む',
})

const emit = defineEmits<{ load: [text: string]; error: [message: string] }>()

async function pick() {
  try {
    const text = await pickTextFile(props.accept)
    // null = 選ばずに閉じた。何も起きなかったことにする
    if (text !== null) emit('load', text)
  } catch (e) {
    emit('error', e instanceof Error ? e.message : String(e))
  }
}
</script>

<template>
  <button type="button" class="text-button" @click.stop.prevent="pick">
    {{ label }}
  </button>
</template>
