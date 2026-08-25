/**
 * 書き出した本文をファイルに保存し、ファイルから読み戻すための小物。
 *
 * **貼り付け経路(コピー / テキストエリア)は残したまま、ファイル経路を並べて足す。**
 * スマホでは貼り付けが速く、PC では長い JSON をファイルで扱うほうが確実なので、
 * どちらか一方に寄せない。書き出しと読み込みで同じ本文を通すのは貼り付け経路と同じ
 * (ファイルにしても「書き出したものがそのまま読み込みの入力」の関係は変わらない)。
 */

/** テキストをファイルとして保存する(`<a download>` を作って踏む)。 */
export function saveTextFile(filename: string, text: string, mime = 'text/plain'): void {
  const url = URL.createObjectURL(new Blob([text], { type: `${mime};charset=utf-8` }))
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  // DOM に入っていないと click() が効かない環境があるので、挿してから踏んで外す
  document.body.appendChild(link)
  link.click()
  link.remove()
  // click() の直後に revoke すると保存が始まらないことがあるので 1 拍置く
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

/**
 * ファイル選択を開き、選ばれたファイルの中身を返す。**選ばずに閉じたら null**。
 *
 * 取り消しは `cancel` イベントで拾う。拾えない環境では解決しないままになるが、
 * 呼び出し側は待っている間 UI を止めないので、選び直せば次の Promise が解決する。
 */
export function pickTextFile(accept: string): Promise<string | null> {
  return new Promise((resolve, reject) => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = accept
    input.style.display = 'none'
    document.body.appendChild(input)

    const finish = (value: string | null) => {
      input.remove()
      resolve(value)
    }
    input.addEventListener('cancel', () => finish(null))
    input.addEventListener('change', () => {
      const file = input.files?.[0]
      if (!file) return finish(null)
      file.text().then(finish, (e) => {
        input.remove()
        reject(e)
      })
    })
    input.click()
  })
}

/** 書き出すファイル名。日付を入れて、世代を並べても見分けられるようにする。 */
export function stampedName(prefix: string, extension: string): string {
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${prefix}-${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}.${extension}`
}
