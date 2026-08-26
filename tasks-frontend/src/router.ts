import { createRouter, createWebHistory } from 'vue-router'

/**
 * 画面の居場所。外に出す面(chiezo-tasks)はルート直下だが、本体(chiezo-app)に
 * 埋め込まれるときは `/tasks/` の下になり、殻に `<base href="/tasks/">` が
 * 差し込まれてくる。ビルドを 1 つに保つため、ここで実行時に読む。
 */
const base = document.querySelector('base')?.getAttribute('href') ?? '/'

export const router = createRouter({
  history: createWebHistory(base),
  routes: [
    { path: '/', name: 'home', component: () => import('./views/HomeView.vue') },
    { path: '/login', name: 'login', component: () => import('./views/LoginView.vue') },
    { path: '/done', name: 'done-tasks', component: () => import('./views/DoneTasksView.vue') },
    { path: '/rules', name: 'rules', component: () => import('./views/RulesView.vue') },
    // タスク・プロジェクト・ルールのどれでもないメモ。他のどの画面にも出ない
    { path: '/notes', name: 'notes', component: () => import('./views/NotesView.vue') },
    // タスク一覧・詳細・編集の専用画面は廃止。未完了はトップ、完了は /done、
    // 編集はカードを押してモーダル。古いブックマーク (/tasks 等) はフォールバックでトップへ
    // プロジェクト専用画面は廃止。作成・編集・並び替えはすべてトップで行う。
    // アーカイブしたものだけはトップに出ないので、ここで見て戻す
    {
      path: '/archived',
      name: 'archived-projects',
      component: () => import('./views/ArchivedProjectsView.vue'),
    },
    // 古いブックマーク (/projects 等) はトップへ流す
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
  scrollBehavior: () => ({ top: 0 }),
})
