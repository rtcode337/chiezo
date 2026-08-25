import { onMounted, onUnmounted } from 'vue'

/**
 * 「下に引っ張って更新」の画面ごとの処理。
 * PWA(スタンドアロン表示)にはブラウザのリロード手段が無いため、
 * App.vue がタッチジェスチャを検知してここに登録された処理を呼ぶ。
 * 一覧系のビューはデータの取り直しを登録し、登録が無い画面では
 * App.vue がページ全体のリロードにフォールバックする。
 */
export type RefreshHandler = () => Promise<unknown> | unknown

let current: RefreshHandler | null = null

/** 表示中のビューが自分の再読込処理を登録する。アンマウントで自動解除。 */
export function usePullToRefresh(handler: RefreshHandler): void {
  onMounted(() => {
    current = handler
  })
  onUnmounted(() => {
    if (current === handler) current = null
  })
}

/** App.vue 用。いま登録されている処理(無ければ null)。 */
export function currentRefreshHandler(): RefreshHandler | null {
  return current
}
