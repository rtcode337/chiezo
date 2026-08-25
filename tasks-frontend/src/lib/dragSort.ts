import { nextTick, shallowRef } from 'vue'

/** これだけ押し続けたらドラッグ開始。これより短いタップは通常のクリックとして通す (ms) */
const LONG_PRESS_MS = 400
/** 長押しの成立前に指がこれ以上動いたら「スクロールしたいのだ」と判断して中止する (px) */
const MOVE_TOLERANCE = 8
/** ドラッグ直後の click を握りつぶす保険の有効時間 (ms) */
const CLICK_GUARD_MS = 400

interface DragState<T> {
  /** どのリストをドラッグ中か。トップのように複数のリストが同居する画面で使い分ける */
  key: string
  /** ドラッグ開始時の並び。変化したかの判定に使う */
  original: T[]
  /** ドラッグ中の作業用の並び */
  items: T[]
  /** 掴んでいる要素の現在位置 */
  index: number
  /** 並び替え対象の行を直接の子に持つ要素 */
  container: HTMLElement
  /** 掴んでいる行。指に追従させるため transform を当てる */
  row: HTMLElement
  /** 行の上端・左端から見た、掴んだ位置のオフセット。ここを指に合わせ続ける */
  grabOffsetX: number
  grabOffsetY: number
  /** いま当てている translate。レイアウト上の位置を逆算するのに使う */
  translateX: number
  translateY: number
  /**
   * 掴んだリストが段組みか。広い画面の一覧(下端まで来たら右の段へ折り返す)だけが該当する。
   * 段組みでは行を横にも動かし、挿入位置も X を見て決める(縦だけでは決まらないため)。
   * 掴んだ時点で決める —— ドラッグ中に段の数が変わることは無い。
   */
  multiColumn: boolean
  /** ポインタが乗っている移動先。`data-drop-zone` を持つ最も近い祖先 */
  zone: Zone | null
}

/** ポインタの下にある `data-drop-zone` の要素 */
interface Zone {
  id: string
  el: HTMLElement
  pointerX: number
  pointerY: number
}

/**
 * 別のリストへ落としたときに {@link useDragSort} の onDrop へ渡るもの。
 * 元のリストの上で離した場合は渡らない(null)。
 */
export interface DropTarget<T> {
  /** 落とした先の `data-drop-zone` の値 */
  id: string
  /** 落とした先の要素。中のどこに入るかは呼び出し側がポインタの位置から決める */
  el: HTMLElement
  /** 離した時点のポインタ。段組みの一覧では X も要る */
  pointerX: number
  pointerY: number
  /** 運んでいたもの */
  item: T
}

/**
 * 段組みされているリストか。左端の違う要素が混ざっていれば段組み。
 * 広い画面の一覧(上から詰めて画面の下端で右の段へ折り返す)だけが該当し、
 * 縦 1 列のリストは全部の左端が揃うので false。
 */
export function isMultiColumn(rows: HTMLElement[]): boolean {
  if (rows.length < 2) return false
  const left = rows[0].getBoundingClientRect().left
  return rows.some((row) => Math.abs(row.getBoundingClientRect().left - left) >= 1)
}

/**
 * ポインタの位置が、並んでいる行のどこに入るかを返す(0 〜 rows.length)。
 *
 * 縦 1 列なら「中心線より上か」だけで決まる。段組みは**上から下、次に右の段**の順に
 * 流れるので、**左の段にいるならその時点で前**、同じ段なら中心線より上かで決める。
 * 「同じ段で中心線より上か」だけを見てはいけない —— 段の一番下にポインタがあると
 * どの枚にも当たらず、末尾まで滑り落ちる。
 */
export function insertionIndex(rows: HTMLElement[], pointerX: number, pointerY: number): number {
  const columns = isMultiColumn(rows)
  for (let i = 0; i < rows.length; i++) {
    const rect = rows[i].getBoundingClientRect()
    const before = columns
      ? pointerX < rect.left ||
        (pointerX < rect.right && pointerY < rect.top + rect.height / 2)
      : pointerY < rect.top + rect.height / 2
    if (before) return i
  }
  return rows.length
}

/** 長押し待ち。タイマーが発火するまでは、ただのタップかスクロールかもしれない */
interface Pending<T> {
  key: string
  list: T[]
  index: number
  container: HTMLElement
  row: HTMLElement
  pointerId: number
  x: number
  y: number
  timer: number
}

// ドラッグ中は「下に引っ張って更新」と食い合うので、App.vue から見えるようにしておく
let dragActive = false

export function isDragActive(): boolean {
  return dragActive
}

/**
 * 長押しが成立したあとはページをスクロールさせない。
 * touch-action はジェスチャの開始時に確定してしまい、後から効かせられないため
 * touchmove を非パッシブで捕まえて止める(SortableJS 等と同じ手)。
 */
function preventTouchScroll(event: TouchEvent) {
  event.preventDefault()
}

/**
 * 行を **長押し(ロングタップ)してからドラッグ**で並び替える。
 * Pointer Events なのでマウス・タッチ共通で、行のどこを押しても掴める。
 *
 * ☰ のような掴み代を置かない代わりに、
 * - 長押しの成立前に指が動いたら、ただのスクロールとして見送る
 * - 成立したら軽く振動させ、以降ページはスクロールしない
 * - 指を離した直後の click(リンク遷移・開閉)は 1 回だけ握りつぶす({@link useDragSort} の clickGuard)
 * という手当てが要る。
 *
 * 行に張るのは `@pointerdown`(start)と `@click.capture`(clickGuard)だけでよい。
 * pointermove / pointerup は押している間だけ window で受ける —— 並び替えで Vue が
 * 行の DOM を動かすと、その要素に紐づいたポインタキャプチャが暗黙に解放されてしまい、
 * 行に張ったハンドラでは以降のイベントを取りこぼすため。
 *
 * カード内のボタンやリンク(コピー・✳ ハンドオフ)は `data-no-drag` を付けて除外する
 * —— ✳ は「長押し → Safari で開く」を一度やってもらう必要があるため、長押しを奪ってはいけない。
 */
export function useDragSort<T>(
  onDrop: (key: string, ordered: T[], drop: DropTarget<T> | null) => Promise<void> | void,
  options: {
    /**
     * 別のリストへの移動を受け付けるか。true にすると、ドラッグ中に
     * `data-drop-zone` を持つ要素を探して {@link dropZone} に出し、
     * 元と違うリストの上で離したとき onDrop の第 3 引数に渡す。
     */
    dropZones?: boolean
  } = {},
) {
  const drag = shallowRef<DragState<T> | null>(null)
  let pending: Pending<T> | null = null
  let swallowClick = false
  let guardTimer = 0
  /** 保存の待ち合わせ中。pointerup と pointercancel が続けて来ても二重保存しないため */
  let finishing = false

  /** 表示に使う並び。ドラッグ中のリストだけ作業用の並びを返す */
  function view(key: string, list: T[]): T[] {
    return drag.value?.key === key ? drag.value.items : list
  }

  function isDragging(key: string, index: number): boolean {
    return drag.value?.key === key && drag.value.index === index
  }

  /** いまドラッグしているリストの鍵。していなければ null */
  function dragKey(): string | null {
    return drag.value?.key ?? null
  }

  /**
   * ポインタが乗っている「元とは違う」リストの `data-drop-zone` 値。
   * 移動先をハイライトするために使う(元のリストの上に居る間は null)。
   */
  function dropZone(): string | null {
    const state = drag.value
    if (!state?.zone || state.zone.id === state.key) return null
    return state.zone.id
  }

  /**
   * 行のルートの pointerdown。`containerSelector` は行から見て最も近い、
   * 並び替え対象の行を子に持つ要素のセレクタ。
   */
  function start(
    key: string,
    list: T[],
    index: number,
    containerSelector: string,
    event: PointerEvent,
  ) {
    if (pending || drag.value) return // 2 本目の指は無視する
    if (event.pointerType === 'mouse' && event.button !== 0) return
    if ((event.target as Element | null)?.closest('[data-no-drag]')) return
    const row = event.currentTarget as HTMLElement
    const container = row.closest(containerSelector)
    if (!(container instanceof HTMLElement)) return
    pending = {
      key,
      list: [...list],
      index,
      container,
      row,
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      timer: window.setTimeout(begin, LONG_PRESS_MS),
    }
    // 押している間は window で追う。行に張ると、並び替えで Vue が行を動かした瞬間に
    // ポインタキャプチャが解放されて以降のイベントを取りこぼす
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', end)
    window.addEventListener('pointercancel', end)
  }

  function unwatchPointer() {
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', end)
    window.removeEventListener('pointercancel', end)
  }

  /** 長押し成立。ここからが本当のドラッグ */
  function begin() {
    const p = pending
    if (!p) return
    pending = null
    dragActive = true
    window.addEventListener('touchmove', preventTouchScroll, { passive: false })
    document.body.style.userSelect = 'none'
    document.body.style.cursor = 'grabbing'
    // マウスで押し込んでいる間に走った選択は消しておく
    document.getSelection()?.removeAllRanges()
    navigator.vibrate?.(10)
    const grabbed = p.row.getBoundingClientRect()
    drag.value = {
      key: p.key,
      original: p.list,
      items: [...p.list],
      index: p.index,
      container: p.container,
      row: p.row,
      grabOffsetX: p.x - grabbed.left,
      grabOffsetY: p.y - grabbed.top,
      translateX: 0,
      translateY: 0,
      multiColumn: isMultiColumn(rowsOf(p.container, p.list.length)),
      zone: null,
    }
    // 掴んだ行を持ち上げて、以降は指に追従させる。
    // pointer-events を切るのは、指の下にあるのが「運んでいる行」自身になってしまい
    // elementFromPoint で移動先を拾えなくなるため(イベントは window で受けるので影響しない)
    p.row.style.position = 'relative'
    p.row.style.zIndex = '2'
    p.row.style.willChange = 'transform'
    p.row.style.pointerEvents = 'none'
  }

  /** ポインタの下にある移動先を拾う。dropZones を有効にしたときだけ動く */
  function findZone(event: PointerEvent): Zone | null {
    if (!options.dropZones) return null
    const under = document.elementFromPoint(event.clientX, event.clientY)
    const el = under?.closest('[data-drop-zone]')
    if (!(el instanceof HTMLElement)) return null
    return { id: el.dataset.dropZone ?? '', el, pointerX: event.clientX, pointerY: event.clientY }
  }

  /**
   * 掴んでいる行を指の位置へ貼り付ける。
   * 並び替えで行のレイアウト位置が変わるたびに当て直す必要があるので、
   * 「レイアウト上の位置(= 現在の rect から今の translate を引いた値)」を基準に計算する。
   */
  function follow(pointerX: number, pointerY: number) {
    const state = drag.value
    // dragActive も見る: end() が持ち上げを戻した後に遅れて呼ばれても当て直さないため
    if (!state || !dragActive) return
    const rect = state.row.getBoundingClientRect()
    const translateY = pointerY - (rect.top - state.translateY + state.grabOffsetY)
    // 縦 1 列のリストでは横に動かさない(指が斜めにぶれてもカードが列から外れないように)
    const translateX = state.multiColumn
      ? pointerX - (rect.left - state.translateX + state.grabOffsetX)
      : 0
    state.row.style.transform = `translate(${translateX}px, ${translateY}px)`
    state.translateX = translateX
    state.translateY = translateY
  }

  /** 並び替え対象の行。コンテナには対象外の要素が続くことがあるので件数で切る */
  function rowsOf(container: HTMLElement, count: number): HTMLElement[] {
    return (Array.from(container.children) as HTMLElement[]).slice(0, count)
  }

  function move(event: PointerEvent) {
    if (pending) {
      // 長押しが成立する前に動いた = スクロールしたいのでドラッグにしない
      if (
        Math.abs(event.clientX - pending.x) > MOVE_TOLERANCE ||
        Math.abs(event.clientY - pending.y) > MOVE_TOLERANCE
      ) {
        cancelPending()
      }
      return
    }
    const state = drag.value
    if (!state) return
    // ハイライトを更新するのは移動先が変わったときだけ(shallowRef の差し替えが要る)。
    // 同じ移動先に留まっている間は、離した位置を測れるよう Y だけ書き換える
    const zone = findZone(event)
    if (zone?.id !== state.zone?.id) {
      drag.value = { ...state, zone }
    } else if (state.zone && zone) {
      state.zone.pointerX = zone.pointerX
      state.zone.pointerY = zone.pointerY
    }
    // 別のリストの上に居る間は、元のリストを掻き回さない
    // (どのみち離せば移動になるので、元の並びが動いて見えるのは邪魔なだけ)
    if (zone && zone.id !== state.key) {
      follow(event.clientX, event.clientY)
      return
    }
    // ポインタがどの行より前に来たかで挿入先を決める(規則は insertionIndex と同じ。
    // ここだけ切り出せないのは、掴んでいる行が指に追従して変位しており、
    // レイアウト上の位置に戻してから測る必要があるため)。
    // 見る行はコンテナの先頭から items の数だけに限る —— トップの `.groups` には
    // 並び替え対象の後ろに対象外の「未分類」グループが同居しており、そこまで数えると
    // items の範囲外の挿入位置が出て、以降の splice が undefined を混ぜて描画が壊れる
    const rows = rowsOf(state.container, state.items.length)
    let target = rows.length - 1
    for (let i = 0; i < rows.length; i++) {
      const rect = rows[i].getBoundingClientRect()
      const dragged = rows[i] === state.row
      const top = dragged ? rect.top - state.translateY : rect.top
      const left = dragged ? rect.left - state.translateX : rect.left
      const before = state.multiColumn
        ? event.clientX < left ||
          (event.clientX < left + rect.width && event.clientY < top + rect.height / 2)
        : event.clientY < top + rect.height / 2
      if (before) {
        target = i
        break
      }
    }
    if (target === state.index || target < 0) {
      follow(event.clientX, event.clientY)
      return
    }
    const items = [...state.items]
    const [moved] = items.splice(state.index, 1)
    items.splice(target, 0, moved)
    // shallowRef なので入れ替えて差し替える
    drag.value = { ...drag.value, items, index: target } as DragState<T>
    // 並び替えで行が別の位置へ動くので、DOM が入れ替わってから貼り直す
    follow(event.clientX, event.clientY)
    nextTick(() => follow(event.clientX, event.clientY))
  }

  /** pointerup / pointercancel 共通 */
  async function end() {
    if (pending) {
      // 長押しの成立前に離した = ただのタップ。click はそのまま通す
      cancelPending()
      return
    }
    const state = drag.value
    if (!state || finishing) return
    finishing = true
    // 持ち上げを戻す(保存の成否によらず、掴んでいた見た目は必ず解除する)
    state.row.style.transform = ''
    state.row.style.position = ''
    state.row.style.zIndex = ''
    state.row.style.willChange = ''
    state.row.style.pointerEvents = ''
    stopDragMode()
    // 離した直後に飛んでくる click(カードのリンク遷移・グループの開閉)を止める
    swallowClick = true
    window.clearTimeout(guardTimer)
    guardTimer = window.setTimeout(() => (swallowClick = false), CLICK_GUARD_MS)
    // 別のリストの上で離したなら、元のリストの並びが変わっていなくても呼び出し側に渡す。
    // drag.value は move() で差し替わっているので、移動先は state ではなくそちらから取る
    const zone = drag.value?.zone ?? null
    const drop: DropTarget<T> | null =
      zone && zone.id !== state.key ? { ...zone, item: state.items[state.index] } : null
    const changed = state.items.some((item, i) => state.original[i] !== item)
    try {
      if (changed || drop) await onDrop(state.key, state.items, drop)
    } finally {
      // 保存が終わってから表示を戻す(ちらつき防止)。失敗時は元の並びに戻る。
      // onDrop が投げてもここを通さないとドラッグ中の表示のまま固まる
      drag.value = null
      finishing = false
    }
  }

  /** 行のルートに `@click.capture` で刺す。ドラッグ直後の 1 回だけ握りつぶす */
  function clickGuard(event: MouseEvent) {
    if (!swallowClick) return
    swallowClick = false
    event.preventDefault()
    event.stopPropagation()
  }

  function cancelPending() {
    if (!pending) return
    window.clearTimeout(pending.timer)
    pending = null
    unwatchPointer()
  }

  function stopDragMode() {
    dragActive = false
    unwatchPointer()
    window.removeEventListener('touchmove', preventTouchScroll)
    document.body.style.userSelect = ''
    document.body.style.cursor = ''
  }

  return { view, isDragging, dragKey, dropZone, start, clickGuard }
}
