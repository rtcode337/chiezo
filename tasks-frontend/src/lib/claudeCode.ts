import type { Task } from '@/api/types'

// Web 版 Claude Code のプリフィル上限は約 5,000 文字。余裕を見て手前で切る
const PROMPT_LIMIT = 4500

/** GitHub のリポジトリ URL を owner/repo スラッグに変換する。GitHub 以外は null */
export function githubSlug(url: string): string | null {
  const m = url
    .trim()
    .match(/^(?:https?:\/\/(?:www\.)?|git@)github\.com[/:]([^/\s]+)\/([^/\s]+?)(?:\.git)?\/?$/i)
  return m ? `${m[1]}/${m[2]}` : null
}

/** GitHub の URL か owner/repo スラッグを owner/repo に正規化する。どちらでもなければ null */
export function repoSlug(value: string): string | null {
  const fromUrl = githubSlug(value)
  if (fromUrl) return fromUrl
  const m = value.trim().match(/^([^/\s:]+)\/([^/\s]+?)(?:\.git)?$/)
  return m ? `${m[1]}/${m[2]}` : null
}

/**
 * タスク内容をプリフィルした Claude Code の URL を組み立てる。
 * スマホでの開き方(ユニバーサルリンク対策・PWA の x-safari- スキーム)は
 * ClaudeCodeButton 側で吸収する。ここは素の https URL を返す。
 * repositories は GitHub の URL だけを owner/repo に変換して渡す
 * (それ以外の URL は Claude 側が解釈できないため落とす)。
 * rulesRepo(ルール画面で設定する規約リポジトリ)は常に足す —— セッションに
 * 含まれたリポジトリはルート直下の CLAUDE.md が読み込まれるため、
 * そこに連結ルールを置いておけば全セッションに共通ルールが効く。
 */
export function claudeCodeUrl(task: Task, repoUrls?: string[], rulesRepo?: string | null): string {
  // タスク番号は付けない —— Claude Code 側からこの画面を参照できないので、
  // 番号を渡しても意味が無いため。かわりに規約リポジトリが設定されていれば
  // 「まず共通ルールに従う」の一言を先頭に添える(セッションに含めるだけでは
  // 他リポジトリの説明と誤読されうるので、従う対象だと明示する)
  const rules = rulesRepo ? repoSlug(rulesRepo) : null
  const lines = rules
    ? [`まず、セッションに含まれる規約リポジトリ ${rules} の CLAUDE.md(共通ルール)に従ってください。`, '', task.title]
    : [task.title]
  let prompt = lines.join('\n')
  if (prompt.length > PROMPT_LIMIT) {
    prompt = `${prompt.slice(0, PROMPT_LIMIT)}\n…(以下略。全文はやること画面のタスクを参照)`
  }

  const params = new URLSearchParams({ prompt })
  const slugs = (repoUrls ?? []).map(githubSlug).filter((s): s is string => s !== null)
  if (rules !== null && !slugs.includes(rules)) slugs.push(rules)
  if (slugs.length > 0) params.set('repositories', slugs.join(','))
  return `https://claude.ai/code?${params.toString()}`
}
