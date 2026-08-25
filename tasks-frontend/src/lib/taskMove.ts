import { useTaskStore } from '@/stores/tasks'
import { insertionIndex } from '@/lib/dragSort'
import type { DropTarget } from '@/lib/dragSort'
import { UNLINKED_KEY } from '@/lib/groups'
import type { TaskGroup } from '@/lib/groups'
import { UNLINK_PROJECT_ID } from '@/api/types'
import type { Task } from '@/api/types'

/**
 * 移動先のカードリストで、ポインタの位置がどこに入るかを返す。
 * 判定は並び替えと同じ {@link insertionIndex}(広い画面の段組みでは X も見る)。
 * タスクが 1 件も無いグループには `.cards` が無く、折りたたまれていれば測れないので、
 * どちらも末尾(= 空なら 0)に入れる。
 */
function insertIndexAt(zone: HTMLElement, pointerX: number, pointerY: number): number {
  const cards = zone.querySelector('.cards')
  if (!(cards instanceof HTMLElement)) return 0
  const rows = Array.from(cards.children) as HTMLElement[]
  if (cards.offsetParent === null) return rows.length
  return insertionIndex(rows, pointerX, pointerY)
}

/**
 * 別のグループまで運ばれたタスクを、そのプロジェクトへ移す(未分類へ運べば紐づけを外す)。
 * 落とした位置にそのまま入るよう、移動先のグループを並び替え直す。
 *
 * 移動したら true。移動先が見つからない場合は false を返すので、
 * 呼び出し側は元のグループ内の並び替えとして続ければよい。
 */
export async function moveTaskToProject(
  drop: DropTarget<Task>,
  groups: TaskGroup[],
): Promise<boolean> {
  const target = groups.find((g) => g.key === drop.id)
  if (!target) return false
  if (!target.project && target.key !== UNLINKED_KEY) return false

  const task = drop.item
  const insertAt = insertIndexAt(drop.el, drop.pointerX, drop.pointerY)
  const ids = target.tasks.filter((t) => t.id !== task.id).map((t) => t.id)
  ids.splice(insertAt, 0, task.id)

  const tasks = useTaskStore()
  // 未分類へ戻すときは 0 を送る(undefined だと「変更しない」の意味になる)
  await tasks.update(task.id, { projectId: target.project?.id ?? UNLINK_PROJECT_ID })
  await tasks.reorder(target.project?.id ?? null, ids)
  return true
}
