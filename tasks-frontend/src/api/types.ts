/** 未着手(todo)・着手中(in_progress)・完了(done)。遷移に制約は無い */
export type TaskStatus = 'todo' | 'in_progress' | 'done'

export interface Project {
  id: number
  name: string
  repoUrls: string[]
  description?: string | null
  archived: boolean
  /** 手動並び替えの表示順(昇順) */
  sortOrder: number
  createdAt: string
  updatedAt: string
}

/** 更新時に projectId へこれを送ると紐づけを外す(未分類に戻す)。null は「変更しない」 */
export const UNLINK_PROJECT_ID = 0

export interface Task {
  id: number
  /** 未分類(どのプロジェクトにも紐づいていない)なら欠落する(JSON は null を落とす設定) */
  projectId?: number | null
  title: string
  status: TaskStatus
  /** 「修正が大変そう」の印。付いたカードは一覧で水色になる。状態(status)とは別軸 */
  flagged: boolean
  /** プロジェクト内の手動並び順(昇順)。0 = 未並び替えでグループの先頭 */
  sortOrder: number
  createdAt: string
  updatedAt: string
}

export interface TaskDetail extends Task {
  /** 未分類なら欠落する */
  projectName?: string
}

/** すべての Claude Code 環境に効かせたい共通ルールの 1 本。本文は Markdown。 */
export interface Rule {
  id: number
  title: string
  body: string
  /** false のルールは連結に含めない */
  enabled: boolean
  /** 手動並び替えの表示順(昇順)。連結の順にもなる */
  sortOrder: number
  createdAt: string
  updatedAt: string
}

export interface RuleInput {
  title?: string
  body?: string
  enabled?: boolean
}

/** 書き出した 1 タスク。id も並び順も持たない(復元先で採番する) */
export interface TaskExportItem {
  title: string
  status: TaskStatus
  /** 印。この列より前に書き出したファイルには入っていない */
  flagged?: boolean
}

/** プロジェクトと、そこに属する未完了タスク */
export interface TaskExportProject {
  name: string
  repoUrls: string[]
  tasks: TaskExportItem[]
}

/** 未完了タスクの書き出し。これがそのまま読み込みの入力になる */
export interface TaskExport {
  version: number
  exportedAt?: string
  projects: TaskExportProject[]
  unassignedTasks: TaskExportItem[]
}

/** 読み込み結果。dryRun でも同じ形で「作る/飛ばす予定」が返る */
export interface TaskImportResult {
  /** 無かったので作る(作った)プロジェクト名 */
  createdProjects: string[]
  /** 作る(作った)タスク。「プロジェクト名 / タイトル」 */
  createdTasks: string[]
  /** 既にある、またはファイル内で重複していて飛ばすタスク */
  skippedTasks: string[]
}

/** 連結ルールを貼り付けて一覧へ戻した結果 */
export interface ImportRulesResult {
  /** 取り込む(取り込んだ)見出し。dryRun でも返る */
  titles: string[]
  /** 取り込み後の全件。dryRun では空 */
  rules: Rule[]
}

/** ルール画面の設定。規約リポジトリは連結ルールを CLAUDE.md として置く先で、✳ ハンドオフに常に含める */
export interface RuleSettings {
  /** GitHub URL か owner/repo スラッグ。未設定なら欠落/null */
  rulesRepoUrl?: string | null
}

export interface Me {
  email: string
  name?: string | null
  pictureUrl?: string | null
  /**
   * 本体(chiezo-app)に埋め込まれて動いているか。LAN 内・認証なしの面なので、
   * ログインもログアウトも無い代わりに管理画面へ戻れる。
   */
  embedded?: boolean
}

export interface Paged<T> {
  items: T[]
  total: number
  page: number
  size: number
  totalPages: number
}

export interface TaskInput {
  /** 更新では undefined = 変更しない、{@link UNLINK_PROJECT_ID}(0) = 紐づけを外す */
  projectId?: number
  title?: string
  status?: TaskStatus
  /** 「修正が大変そう」の印。undefined = 変更しない */
  flagged?: boolean
}

export interface ProjectInput {
  name?: string
  /** undefined = 変更しない、空配列 = 全部消す */
  repoUrls?: string[]
  description?: string | null
  archived?: boolean
}

/**
 * そのほかのメモ(タスク・プロジェクト・ルールのどれでもない短期記憶)。
 * 読むだけの型で、書く口は画面に持たない。
 */
export interface Note {
  id: number
  title: string
  body: string
  tags: string[]
  createdAt: string
  updatedAt: string
  /** 本体のブラウズ画面。全文と生の項目はあちらで見る */
  url: string
}

export interface NoteTag {
  tag: string
  docs: number
}
