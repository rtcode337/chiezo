/**
 * モーダルの背景(オーバーレイ)をクリックしたときだけ閉じるためのハンドラ。
 * 戻り値を背景の要素に `v-on="..."` でそのまま渡す(setup で 1 回だけ作ること)。
 *
 * 背景に `@click.self="close"` を置くと、**モーダル内のテキストを範囲選択して
 * マウスを背景の上まで動かして離しただけで閉じてしまう**。click は mousedown と
 * mouseup の共通祖先で発火するため、選択の始点が中身であっても target が背景になり、
 * `.self`(= target === currentTarget)の判定を通ってしまうため。
 * mousedown も背景の上で始まったときだけ閉じることでこれを防ぐ。
 */
export function backdropClose(close: () => void) {
  let pressedOnBackdrop = false

  return {
    mousedown(e: MouseEvent) {
      pressedOnBackdrop = e.target === e.currentTarget
    },
    click(e: MouseEvent) {
      if (e.target !== e.currentTarget || !pressedOnBackdrop) return
      pressedOnBackdrop = false
      close()
    },
  }
}
