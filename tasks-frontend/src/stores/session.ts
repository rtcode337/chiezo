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

  async function load(): Promise<boolean> {
    try {
      me.value = await api.me()
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

  return { me, checked, load, reset, login, logout }
})
