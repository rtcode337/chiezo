import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api, UnauthorizedError } from '@/api/client'
import type { Me } from '@/api/types'

/**
 * ログイン状態。/api/me が 401 ならログイン画面へ誘導する (仕様書 §6.1)。
 */
export const useSessionStore = defineStore('session', () => {
  const me = ref<Me | null>(null)
  const checked = ref(false)
  /**
   * 本体(chiezo-app)に埋め込まれて動いているか(認証を持たない面)。
   * **ログイン状態とは別に持つ** —— これはサーバーの構成であって、通信の成否や
   * セッションの有無で変わらない。ヘッダーが利用者メニューの代わりに管理画面への
   * 戻り口を出すかの判断に使う。
   */
  const embedded = ref(false)

  async function load(): Promise<boolean> {
    try {
      me.value = await api.me()
      embedded.value = me.value.embedded === true
      return true
    } catch (error) {
      if (error instanceof UnauthorizedError) {
        me.value = null
        return false
      }
      throw error
    } finally {
      checked.value = true
    }
  }

  /**
   * ログイン状態を手元だけ破棄する(サーバーは呼ばない)。
   * 通信失敗でログイン画面へ戻すときに App.vue が使う。
   */
  function reset() {
    me.value = null
    checked.value = true
  }

  async function logout() {
    await api.logout().catch(() => undefined)
    me.value = null
    window.location.assign('/')
  }

  function login() {
    // Spring Security の認可エンドポイントへ。SPA 内遷移ではなく全体遷移させる
    window.location.assign('/oauth2/authorization/google')
  }

  return { me, checked, embedded, load, reset, login, logout }
})
