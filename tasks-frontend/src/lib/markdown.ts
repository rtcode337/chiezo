/**
 * ノートやタスク本文を表示するための最小 Markdown レンダラ。
 *
 * 方針: まず HTML を完全にエスケープし、その後で決めた記法だけをタグに戻す。
 * これで入力に HTML が混ざっても素通しにならない。外部ライブラリは足さない
 * (バンドルを小さく保ちたい & CSP を 'self' のままにしたい)。
 *
 * 対応する記法: 見出し / 箇条書き / 番号付きリスト / 引用 / コードブロック /
 * インラインコード / 強調 / 打ち消し / リンク / 水平線。
 */

const ESCAPES: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}

function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (ch) => ESCAPES[ch]!)
}

/** javascript: などを弾く。 */
function safeUrl(url: string): string | null {
  const trimmed = url.trim()
  if (/^(https?:\/\/|mailto:|\/|#)/i.test(trimmed)) {
    return trimmed
  }
  return null
}

function inline(text: string): string {
  let html = escapeHtml(text)

  // `code` は他の記法より先に退避して、中身を装飾されないようにする。
  // 目印には本文に現れない制御文字を使う
  const codes: string[] = []
  html = html.replace(/`([^`]+)`/g, (_, code: string) => {
    codes.push(code)
    return `\u0000${codes.length - 1}\u0000`
  })

  html = html.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (match, label: string, url: string) => {
    const href = safeUrl(url)
    if (!href) return match
    return `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`
  })
  html = html.replace(/(^|[^*])\*\*([^*]+)\*\*/g, '$1<strong>$2</strong>')
  html = html.replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>')
  html = html.replace(/~~([^~]+)~~/g, '<del>$1</del>')

  return html.replace(/\u0000(\d+)\u0000/g, (_, index: string) => `<code>${codes[Number(index)]}</code>`)
}

export function renderMarkdown(source: string): string {
  const lines = source.replace(/\r\n?/g, '\n').split('\n')
  const out: string[] = []
  let listType: 'ul' | 'ol' | null = null
  let inCodeBlock = false
  let inQuote = false
  let paragraph: string[] = []

  const closeList = () => {
    if (listType) {
      out.push(`</${listType}>`)
      listType = null
    }
  }
  const closeQuote = () => {
    if (inQuote) {
      out.push('</blockquote>')
      inQuote = false
    }
  }
  const flushParagraph = () => {
    if (paragraph.length) {
      out.push(`<p>${paragraph.map(inline).join('<br>')}</p>`)
      paragraph = []
    }
  }
  const closeAll = () => {
    flushParagraph()
    closeList()
    closeQuote()
  }

  for (const line of lines) {
    const fence = /^```/.test(line)
    if (fence) {
      if (inCodeBlock) {
        out.push('</code></pre>')
        inCodeBlock = false
      } else {
        closeAll()
        out.push('<pre><code>')
        inCodeBlock = true
      }
      continue
    }
    if (inCodeBlock) {
      out.push(escapeHtml(line) + '\n')
      continue
    }

    if (!line.trim()) {
      closeAll()
      continue
    }

    const heading = /^(#{1,4})\s+(.*)$/.exec(line)
    if (heading) {
      closeAll()
      const level = heading[1]!.length + 1 // ページ内なので h2 から始める
      out.push(`<h${level}>${inline(heading[2]!)}</h${level}>`)
      continue
    }

    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) {
      closeAll()
      out.push('<hr>')
      continue
    }

    const quote = /^>\s?(.*)$/.exec(line)
    if (quote) {
      flushParagraph()
      closeList()
      if (!inQuote) {
        out.push('<blockquote>')
        inQuote = true
      }
      out.push(`<p>${inline(quote[1]!)}</p>`)
      continue
    }
    closeQuote()

    const bullet = /^\s*[-*+]\s+(.*)$/.exec(line)
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line)
    if (bullet || numbered) {
      flushParagraph()
      const wanted = bullet ? 'ul' : 'ol'
      if (listType !== wanted) {
        closeList()
        out.push(`<${wanted}>`)
        listType = wanted
      }
      const item = (bullet ?? numbered)![1]!
      // Claude Code はチェックリストで進捗を書きがちなので拾う
      const checkbox = /^\[([ xX])\]\s+(.*)$/.exec(item)
      if (checkbox) {
        const checked = checkbox[1] !== ' '
        out.push(
          `<li class="task"><input type="checkbox" disabled${checked ? ' checked' : ''}>` +
            `<span${checked ? ' class="done"' : ''}>${inline(checkbox[2]!)}</span></li>`,
        )
        continue
      }
      out.push(`<li>${inline(item)}</li>`)
      continue
    }
    closeList()

    paragraph.push(line)
  }

  if (inCodeBlock) {
    out.push('</code></pre>')
  }
  closeAll()
  return out.join('')
}
