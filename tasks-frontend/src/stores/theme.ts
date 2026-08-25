import { defineStore } from 'pinia'
import { ref } from 'vue'
import { applyTheme, getStoredTheme, persistTheme } from '@/lib/theme'
import type { Theme } from '@/lib/theme'

export const useThemeStore = defineStore('theme', () => {
  const theme = ref<Theme>(getStoredTheme())

  function set(next: Theme) {
    theme.value = next
    applyTheme(next)
    persistTheme(next)
  }

  function toggle() {
    set(theme.value === 'dark' ? 'light' : 'dark')
  }

  return { theme, set, toggle }
})
