import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { router } from './router'
import { initTheme } from './lib/theme'
import './assets/main.css'

// 描画前にテーマを当ててちらつきを防ぐ
initTheme()

createApp(App).use(createPinia()).use(router).mount('#app')
