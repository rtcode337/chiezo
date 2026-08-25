import { onBeforeUnmount } from 'vue'
import { isDragActive } from '@/lib/dragSort'
import type { ComponentPublicInstance } from 'vue'

/**
 * 広い画面の段組み(`.column-flow`)の器を、**段の整数倍の幅に詰めて中央へ置く**。
 *
 * 理由は 2 つ。
 *
 * 1. **段は器の幅いっぱいに引き伸ばされる。** CSS の `column-width` は下限でしかなく、
 *    器が中途半端な幅だと 1 段が指定より太くなる(1920px に 46rem の段なら 2 段 × 58rem)。
 *    トップでは入力欄も同じ流れに入るので、それが伸びると入力欄まで間延びする。
 * 2. **段組みは左から詰める。** 折り返しが要らない量のときは画面の左端に段が残って
 *    右がまるごと空くので、使っている段のぶんまで器を詰めて中央へ寄せる。
 *
 * 幅は決め打ちにせず**測ってから決める** —— 決め打ちで狭めると、量が増えたときに
 * 横スクロールが早く始まる。段組みが効いている画面幅かも **CSS から読む**
 * (効いていれば `column-width` が px になる)。境目の値を JS 側にも書くと、
 * CSS を動かしたときに片方だけ古くなる。
 *
 * 返るのは ref のコールバック。テンプレートでは `:ref="flow"` で渡す ——
 * `<script setup>` の文字列 ref は ref オブジェクトを取り出せず、
 * `:ref` に ref オブジェクトを渡すとテンプレート側で中身に展開されてしまうため。
 */
export function useColumnFlow() {
  let node: HTMLElement | null = null
  let frame = 0
  // 中身が増減すれば使う段の数も変わる。style の書き換えは監視しない(自分で書くため)
  const observer = new MutationObserver(() => schedule())

  /** 段をまたぐ要素の矩形は全フラグメントを含むので、右端がそのまま使用幅になる */
  function usedWidth(target: HTMLElement): number {
    const left = target.getBoundingClientRect().left
    let used = 0
    for (const child of Array.from(target.children)) {
      used = Math.max(used, child.getBoundingClientRect().right - left + target.scrollLeft)
    }
    return used
  }

  function apply() {
    if (!node) return
    // 並び替えの最中は測らない(掴んでいるカードの下で幅が動くと落ち先を見失う)
    if (isDragActive()) return
    // いったん制限を外し、素の幅で組み直したところを測る。
    // 同じフレームの中で戻すので、途中の幅が描画されることはない
    node.style.width = ''
    node.style.marginInline = ''
    const style = getComputedStyle(node)
    const column = Number.parseFloat(style.columnWidth)
    if (!Number.isFinite(column)) return // 段組みをしない幅(column-width: auto)
    const gap = Number.parseFloat(style.columnGap) || 0
    const pitch = column + gap

    // 器に入る段の数。1 段ぶんにも足りなくても 1 段は出す
    const fits = Math.max(1, Math.floor((node.clientWidth + gap) / pitch))
    // まず段が伸びない幅(整数倍)にしてから、何段使ったかを測る
    node.style.width = `${fits * pitch - gap}px`
    node.style.marginInline = 'auto'
    const columns = Math.min(fits, Math.max(1, Math.ceil((usedWidth(node) + gap) / pitch)))
    node.style.width = `${columns * pitch - gap}px`
  }

  function schedule() {
    cancelAnimationFrame(frame)
    frame = requestAnimationFrame(apply)
  }

  /** 一覧は v-if/v-else で出し入れするので、付け外しはこのコールバックで面倒を見る */
  function attach(target: Element | ComponentPublicInstance | null) {
    observer.disconnect()
    node = target instanceof HTMLElement ? target : null
    if (!node) return
    observer.observe(node, { childList: true, subtree: true })
    schedule()
  }

  window.addEventListener('resize', schedule)
  onBeforeUnmount(() => {
    cancelAnimationFrame(frame)
    window.removeEventListener('resize', schedule)
    observer.disconnect()
  })

  return attach
}
