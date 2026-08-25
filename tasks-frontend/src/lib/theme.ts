/**
 * 明暗テーマの適用と永続化。デフォルトは明るいテーマ。
 * <html data-theme="..."> を切り替え、実際の配色は main.css の CSS 変数が受け持つ。
 */
export type Theme = 'light' | 'dark'

const STORAGE_KEY = 'chiezo-tasks-theme'

/** PWA のステータスバー色 (index.html の meta[name=theme-color]) も合わせて更新する。 */
const THEME_COLORS: Record<Theme, string> = {
  light: '#f4f6f9',
  dark: '#12181f',
}

export function getStoredTheme(): Theme {
  try {
    return localStorage.getItem(STORAGE_KEY) === 'dark' ? 'dark' : 'light'
  } catch {
    // プライベートブラウズ等で localStorage が使えない場合はデフォルトへ
    return 'light'
  }
}

export function applyTheme(theme: Theme): void {
  document.documentElement.setAttribute('data-theme', theme)
  const meta = document.querySelector('meta[name="theme-color"]')
  meta?.setAttribute('content', THEME_COLORS[theme])
}

export function persistTheme(theme: Theme): void {
  try {
    localStorage.setItem(STORAGE_KEY, theme)
  } catch {
    // 保存できなくてもその場の切り替えは効くので握りつぶす
  }
}

/** 起動時に一度呼ぶ。描画前に data-theme を当ててちらつきを防ぐ。 */
export function initTheme(): void {
  applyTheme(getStoredTheme())
}
