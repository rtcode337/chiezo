import { readonly, ref } from 'vue'

/**
 * API 通信の進行状況をアプリ全体で共有する。
 * client.ts がリクエストの開始・終了を数え、App.vue が「通信中」オーバーレイの表示に使う。
 * client.ts から router やストアへ依存を張らないための橋渡しでもある
 * (通信失敗時の誘導は App.vue が {@link onRequestFailure} で登録する)。
 */
const count = ref(0)

/** 進行中の API リクエスト数。0 より大きい間は通信中。 */
export const inFlightRequests = readonly(count)

export function beginRequest() {
  count.value += 1
}

export function endRequest() {
  // 二重呼び出しなどで負に振れても「通信していないのにオーバーレイが消えない」を起こさない
  count.value = Math.max(0, count.value - 1)
}

// 通信失敗(リクエストが届かない・応答が返らない)の通知先
let failureHandler: (() => void) | null = null

/** 通信失敗時の処理を登録する。App.vue がダイアログ表示とログイン画面への誘導を登録する。 */
export function onRequestFailure(handler: () => void) {
  failureHandler = handler
}

export function notifyRequestFailure() {
  failureHandler?.()
}
