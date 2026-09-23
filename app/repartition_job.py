"""区画の割り直しを、**押した人を待たせずに**走らせる。

**HTTP の裏に置いたままにできない仕事だった。** 割り直しは母集団を丸ごと 1 周
舐めて全点をメモリに載せる —— 本番の食事処は 686,602 件あり、押すとブラウザ
(とリバースプロキシ)が先に切れる。**切れてもサーバー側のスレッドは止まらない**
ので処理自体は最後まで走るが、押した人からは:

- **終わったのか分からない**(進み具合も結果も画面に出ない)
- **もう一度押せてしまう** —— 2 本が同時に同じ母集団を読むので、メモリも時間も倍
- **落ちたのか走っているのかが区別できない**

ので、**状態を控えて画面に出す**形にする。

**控えは設定の置き場に持つ**(`app/machine_store.py`)。`--workers 2` なので、
プロセスの中の変数だと**押したワーカーと画面を出すワーカーが別のとき**に
何も見えない(`app/media.py` のジョブと同じ理由)。

**定義とは別の鍵にする** —— 収集の定義は `collect/<名前>` に並ぶので、そこへ
状態を混ぜると「repartition」という名前の収集と鍵がぶつかる。
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from app import collect, machine_store

log = logging.getLogger("chiezo.app")

KIND = "collect_job"
KEY = "repartition"

# 走っていると見なす上限。**これを過ぎた「走っている」は落ちたものとして読む** ——
# 面倒を見ているワーカーが消えても控えは残るので、印が無いと永遠に「走っている」
# ままになる(`app/media.py` が古い running を畳むのと同じ理由)。
# 割り直しは母集団を 1 周舐めるだけなので、数分で終わる仕事に余裕を見た数
STALE_AFTER = timedelta(minutes=30)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


def _all() -> dict:
    if not machine_store.is_enabled():
        return {}
    body = machine_store.get(KIND, KEY)
    if not body:
        return {}
    try:
        found = json.loads(body)
    except ValueError:
        # **読めなければ空から始める。** 控えは次に押せば作り直せる(行列と同じ判断)
        log.warning("repartition state is unreadable; starting over")
        return {}
    return found if isinstance(found, dict) else {}


def _save(state: dict) -> None:
    if machine_store.is_enabled():
        machine_store.put(KIND, KEY, json.dumps(state, ensure_ascii=False, indent=2))


def state(name: str) -> dict:
    """その収集の割り直しの様子。押したことがなければ空。

    **時間切れの「走っている」は落ちたものとして返す** —— 面倒を見ているワーカーが
    消えても控えは残るので、印が無いと永遠に走っているように見える。
    """
    found = _all().get(name)
    if not isinstance(found, dict):
        return {}
    if found.get("state") != "running":
        return found
    at = found.get("started_at") or ""
    with_when = datetime.fromisoformat(at) if at else None
    if with_when is not None and _now() - with_when > STALE_AFTER:
        return {**found, "state": "error",
                "error": "途中で止まりました(もう一度押してください)"}
    return found


def running(name: str) -> bool:
    """いまその収集を割り直している最中か。**二度押しを止めるのに使う**。"""
    return state(name).get("state") == "running"


def start(name: str) -> None:
    """走り始めたことを控える。**起こす前に書く** —— 書く前に走らせると、
    終わってから控えを書く一瞬のあいだに、画面は「押していない」ように見える。"""
    found = _all()
    found[name] = {"state": "running", "started_at": _iso(_now())}
    _save(found)


def finish(name: str, count: int) -> None:
    """割り直せたことを控える。**区画の数も残す** —— 「押したら何区画になったか」は
    そのあと台帳を開かなくても読めたほうがよい。"""
    found = _all()
    found[name] = {
        "state": "done", "finished_at": _iso(_now()), "partitions": int(count),
        "started_at": str((found.get(name) or {}).get("started_at") or ""),
    }
    _save(found)


def fail(name: str, why: str) -> None:
    """落ちたことを控える。**理由も残す** —— 無人で走るわけではないが、
    押した人はその場に居ないことがある(数分かかる)。"""
    found = _all()
    found[name] = {
        "state": "error", "finished_at": _iso(_now()), "error": str(why)[:300],
        "started_at": str((found.get(name) or {}).get("started_at") or ""),
    }
    _save(found)


def run(name: str, sources: dict) -> None:
    """割り直しを 1 本走らせて、結果を控える。**呼ぶ側は待たない**。

    **落ちても控えに残す。** 例外をそのまま投げても、受ける HTTP はもう返って
    いるので誰も読まない —— 画面に出す唯一の道がここ。
    """
    try:
        ledger = collect.repartition(name, sources)
    except Exception as e:
        log.warning("repartition %s failed: %s", name, e, exc_info=True)
        fail(name, getattr(e, "detail", None) or str(e))
        return
    finish(name, len(ledger))
