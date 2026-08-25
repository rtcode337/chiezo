import type {
  ImportRulesResult,
  Me,
  Paged,
  Project,
  ProjectInput,
  Rule,
  RuleInput,
  RuleSettings,
  Task,
  TaskDetail,
  TaskExport,
  TaskImportResult,
  TaskInput,
  TaskStatus,
} from './types'
import { beginRequest, endRequest, notifyRequestFailure } from '@/lib/network'

/** 未認証。呼び出し側はログイン画面へ誘導する。 */
export class UnauthorizedError extends Error {
  constructor() {
    super('ログインが必要です')
    this.name = 'UnauthorizedError'
  }
}

/** サーバーが返した {"error":{"code","message"}} を運ぶ。 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

/**
 * 通信失敗(オフライン・回線断・タイムアウト = リクエストが届かない/応答が返らない)。
 * client 側で notifyRequestFailure() 済みなので、App.vue がダイアログを出して
 * ログイン画面へ誘導する。呼び出し側での個別ハンドリングは不要。
 */
export class OfflineError extends Error {
  constructor() {
    super('ネットワークに接続できません')
    this.name = 'OfflineError'
  }
}

// バックエンドが応答を返さないときに通信失敗として扱うまでの時間。
// 低速なディスクが眠った環境では初回アクセスに十数秒かかることがあるため
// (CLAUDE.md のウォームアップの項)、短くしすぎない
const REQUEST_TIMEOUT_MS = 30_000

function readCookie(name: string): string | null {
  const hit = document.cookie.split('; ').find((row) => row.startsWith(`${name}=`))
  return hit ? decodeURIComponent(hit.slice(name.length + 1)) : null
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body !== undefined) {
    headers.set('Content-Type', 'application/json')
  }
  // Spring Security の CookieCsrfTokenRepository と対になる
  const csrf = readCookie('XSRF-TOKEN')
  if (csrf && init.method && init.method !== 'GET') {
    headers.set('X-XSRF-TOKEN', csrf)
  }

  // 通信中はオーバーレイを出すため、開始と終了を lib/network.ts で数える
  beginRequest()
  try {
    let response: Response
    try {
      response = await fetch(path, {
        ...init,
        headers,
        credentials: 'same-origin',
        signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      })
    } catch {
      // fetch の失敗もタイムアウトもまとめて通信失敗。App.vue がログイン画面へ誘導する
      notifyRequestFailure()
      throw new OfflineError()
    }

    // リバースプロキシ越しだと、バックエンドが落ちていても fetch は成功して
    // ゲートウェイエラーが返ってくる。これも「応答が返らない」と同じ通信失敗として扱う
    if (response.status === 502 || response.status === 503 || response.status === 504) {
      notifyRequestFailure()
      throw new OfflineError()
    }

    if (response.status === 401) {
      throw new UnauthorizedError()
    }
    if (response.status === 204) {
      return undefined as T
    }
    if (!response.ok) {
      const body = await response.json().catch(() => null)
      const error = body?.error
      throw new ApiError(
        response.status,
        error?.code ?? 'unknown',
        error?.message ?? `リクエストが失敗しました (${response.status})`,
      )
    }
    return (await response.json()) as T
  } finally {
    endRequest()
  }
}

export const api = {
  me: () => request<Me>('/api/me'),

  logout: () => request<void>('/api/logout', { method: 'POST' }),

  listProjects: (archived?: boolean) => {
    const query = archived === undefined ? '?archived=' : `?archived=${archived}`
    return request<Project[]>(`/api/projects${query}`)
  },

  createProject: (input: ProjectInput) =>
    request<Project>('/api/projects', { method: 'POST', body: JSON.stringify(input) }),

  updateProject: (id: number, input: ProjectInput) =>
    request<Project>(`/api/projects/${id}`, { method: 'PATCH', body: JSON.stringify(input) }),

  /** アーカイブ済みのみ。紐づくタスクも一緒に消える。 */
  deleteProject: (id: number) => request<void>(`/api/projects/${id}`, { method: 'DELETE' }),

  /** 並び替え。全プロジェクトの id を望む順で送ると、並び替え後の全件を返す。 */
  reorderProjects: (ids: number[]) =>
    request<Project[]>('/api/projects/order', { method: 'PUT', body: JSON.stringify({ ids }) }),

  listTasks: (params: { projectId?: number; status?: TaskStatus; done?: boolean } = {}) => {
    const query = new URLSearchParams()
    if (params.projectId !== undefined) query.set('projectId', String(params.projectId))
    if (params.status !== undefined) query.set('status', params.status)
    if (params.done !== undefined) query.set('done', String(params.done))
    const suffix = query.toString() ? `?${query}` : ''
    return request<Task[]>(`/api/tasks${suffix}`)
  },

  /** 完了タスクをページングで取得 (10 件/頁 など)。 */
  listDoneTasks: (params: { projectId?: number; page: number; size: number }) => {
    const query = new URLSearchParams({ done: 'true' })
    if (params.projectId !== undefined) query.set('projectId', String(params.projectId))
    query.set('page', String(params.page))
    query.set('size', String(params.size))
    return request<Paged<Task>>(`/api/tasks?${query}`)
  },

  /**
   * プロジェクト内の並び替え。ids を望む順で送ると、並び替えた分を返す。
   * projectId は未紐づけのかたまりなら null。画面に出ている分だけの部分集合でよい。
   */
  reorderTasks: (projectId: number | null, ids: number[]) =>
    request<Task[]>('/api/tasks/order', {
      method: 'PUT',
      body: JSON.stringify({ projectId, ids }),
    }),

  getTask: (id: number) => request<TaskDetail>(`/api/tasks/${id}`),

  createTask: (input: TaskInput) =>
    request<Task>('/api/tasks', { method: 'POST', body: JSON.stringify(input) }),

  updateTask: (id: number, input: TaskInput) =>
    request<Task>(`/api/tasks/${id}`, { method: 'PATCH', body: JSON.stringify(input) }),

  deleteTask: (id: number) => request<void>(`/api/tasks/${id}`, { method: 'DELETE' }),

  /** 未完了タスクの書き出し(プロジェクト名・リポジトリ付き)。返り値をそのまま importTasks に渡せる。 */
  exportTasks: () => request<TaskExport>('/api/tasks/export'),

  /** 書き出したものの読み込み。dryRun なら書き込まず、作る/飛ばす予定だけ返す。 */
  importTasks: (data: TaskExport, dryRun = false) =>
    request<TaskImportResult>(`/api/tasks/import?dryRun=${dryRun}`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  listRules: () => request<Rule[]>('/api/rules'),

  /** 有効なルールを表示順に連結した 1 本の Markdown。 */
  combinedRules: () => request<{ markdown: string }>('/api/rules/combined'),

  createRule: (input: RuleInput) =>
    request<Rule>('/api/rules', { method: 'POST', body: JSON.stringify(input) }),

  updateRule: (id: number, input: RuleInput) =>
    request<Rule>(`/api/rules/${id}`, { method: 'PATCH', body: JSON.stringify(input) }),

  deleteRule: (id: number) => request<void>(`/api/rules/${id}`, { method: 'DELETE' }),

  /** 並び替え。全ルールの id を望む順で送ると、並び替え後の全件を返す。 */
  reorderRules: (ids: number[]) =>
    request<Rule[]>('/api/rules/order', { method: 'PUT', body: JSON.stringify({ ids }) }),

  /**
   * 連結ルールの Markdown を貼り付けて一覧へ戻す(combinedRules の逆)。
   * dryRun なら書き込まず、取り込む見出しだけ返す。
   */
  importRules: (input: { markdown: string; replace?: boolean; dryRun?: boolean }) =>
    request<ImportRulesResult>('/api/rules/import', {
      method: 'POST',
      body: JSON.stringify(input),
    }),

  ruleSettings: () => request<RuleSettings>('/api/rules/settings'),

  /** rulesRepoUrl は空文字で「消す」(null は「変更しない」)。 */
  updateRuleSettings: (input: RuleSettings) =>
    request<RuleSettings>('/api/rules/settings', { method: 'PATCH', body: JSON.stringify(input) }),
}
