"""取り込みの待ち行列と、いま走っている取り込みの読み方。

**取り込みは同時に走らせられる本数が決まっている**(chiezo-trigger の
`CHIEZO_INGEST_SLOTS`。既定 1)。埋まっているときに来た依頼を、ここで待たせる ——
かつては断って「次の周期でもう一度起こしに行く」だけだったので、待っているものが
どこにも見えず、ログには「混んでいるので次の周期へ回します」が並ぶだけだった
(スキップされているのか待っているのか、外からは読めない)。

**並ぶのは収集の回だけ。** ダンプの取り込み(初期化・再構築)は人が押すもので、
埋まっていれば断る(何時間もかかる取り込みを黙って積むと、押した人が
いつ始まるのか読めない)。割り込みも今までどおりその場で起こす。

**どこから来たか(`origin`)で扱いが違う:**

- `schedule` …… 予定の来た巡回。**予定は起こせたときに進める**ので、待っている
  あいだも「予定が来ている」のまま —— 積むのは 1 度だけ(同じ組は 2 度積まない)
- `worker` …… ワーカーが拾った塊の 1 本。**そのワーカーのものは 1 本ずつ流す**
  (同じ相手へ同時に 2 本投げると、ブリッジの側で待たされて時間切れに算入される)
- `manual` …… 画面の「今すぐ実行」や外のアプリからの依頼。**止めてある収集でも
  流す**(押した人の試し撃ちを、無人の側の判断で落とさない)

**置き場は設定の置き場の 1 件**(`machine_store`)。chiezo-app は `--workers 2`
なので、プロセスの中の変数だと、積んだプロセスと流すプロセスが別のときに見えない。
**書くときは読んだときのままなら置く**(`put_if`)—— 2 本の時計が同じ 1 件を
同時に流しに行かないようにするため。

**走らせたものも控える**(`running`)。取り込みは「いつ終わったか」を教えに来ない
ので、trigger の状態から消えたら終わったとみなす(`settle`)。ワーカーが
「流し終えた」を知るのはここから。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

from app import machine_store

log = logging.getLogger("chiezo.app")

KIND = "ingest"
KEY = "queue"
ORIGINS = ("schedule", "worker", "manual")

# 走らせたと控えてから、取り込みの状態に出てくるまでの猶予(秒)。
# trigger は受けた時点で「走っている」に入れるので本来は要らないが、
# **状態を読んだのが起こす前**だと、起こしたばかりの 1 本がまだ載っていない ——
# そこで終わったとみなすと、ワーカーが流し終えたことになって次を流してしまう
SETTLE_GRACE_SECONDS = 90

# 取り合いに負けたときに読み直す回数。2 本の時計が同じ周に書くだけなので、
# 数回で必ず片が付く
_RETRIES = 20


# ---- trigger の状態の読み方 -----------------------------------------------------
#
# **古い trigger は 1 本ぶんの形しか返さない**(`state` / `source`)。取り込みは
# app と別に焼かれるので、片方だけ古いまま、が普通に起きる —— どちらの形でも
# 同じように読めるよう、読み方をここに集める。


def jobs(status: dict | None) -> list[dict]:
    """いま走っている取り込み(古いものから)。**繋がらなければ空**。"""
    if not status or status.get("state") == "unreachable":
        return []
    found = status.get("jobs")
    if isinstance(found, list):
        return [j for j in found if isinstance(j, dict)]
    return [status] if status.get("state") == "running" else []


def slots(status: dict | None) -> int:
    """同時に走らせられる本数。名乗らない trigger は 1 本。"""
    try:
        return max(1, int((status or {}).get("slots") or 1))
    except (TypeError, ValueError):
        return 1


def running_names(status: dict | None) -> list[str]:
    return [str(j.get("source") or "?") for j in jobs(status)]


def is_running(status: dict | None, name: str) -> bool:
    return name in running_names(status)


def is_full(status: dict | None) -> bool:
    """もう 1 本も起こせないか。"""
    return len(jobs(status)) >= slots(status)


def finished(status: dict | None) -> list[dict]:
    """終わった回(新しいものから)。古い trigger は、いまの 1 本が終わっていればそれ。"""
    if not status or status.get("state") == "unreachable":
        return []
    found = status.get("recent")
    if isinstance(found, list):
        return [j for j in found if isinstance(j, dict)]
    return [status] if status.get("state") in ("done", "error", "stopped") else []


# ---- 待ち行列 -----------------------------------------------------------------


def is_enabled() -> bool:
    return machine_store.is_enabled()


def _empty() -> dict:
    return {"waiting": [], "running": []}


def _parse(raw: str | None) -> dict:
    if not raw:
        return _empty()
    try:
        found = json.loads(raw)
    except ValueError:
        # **読めなければ空から始める。** 予定の来た巡回とワーカーのぶんは次の周で
        # 積み直る(人に直させる価値が無い)。落ちるのは画面から押したぶんだけ
        log.warning("ingest queue is unreadable; starting over")
        return _empty()
    if not isinstance(found, dict):
        return _empty()
    return {
        "waiting": [e for e in found.get("waiting") or [] if isinstance(e, dict)],
        "running": [e for e in found.get("running") or [] if isinstance(e, dict)],
    }


def _state() -> dict:
    if not is_enabled():
        return _empty()
    return _parse(machine_store.get(KIND, KEY))


def _mutate(change):
    """読んで・変えて・置く。**読んだときのままなら置く**(取り合いに負けたら読み直す)。

    `change` は状態を書き換えて、呼ぶ側へ返す値を返す。変わらなければ書かない。
    """
    if not is_enabled():
        return change(_empty())
    for _ in range(_RETRIES):
        raw = machine_store.get(KIND, KEY)
        state = _parse(raw)
        before = json.dumps(state, ensure_ascii=False, sort_keys=True)
        result = change(state)
        if json.dumps(state, ensure_ascii=False, sort_keys=True) == before:
            return result
        body = json.dumps(state, ensure_ascii=False, indent=2)
        if machine_store.put_if(KIND, KEY, body, raw):
            return result
    raise RuntimeError("取り込みの待ち行列を書けませんでした(取り合いが続いています)")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def waiting() -> list[dict]:
    """待っているもの(先に積まれた順)。"""
    return _state()["waiting"]


def running() -> list[dict]:
    """こちらが起こして、まだ終わったと分かっていないもの。"""
    return _state()["running"]


def _same(entry: dict, collection: str, sweep: str) -> bool:
    return entry.get("collection") == collection and entry.get("sweep") == sweep


def add(
    collection: str,
    sweep: str,
    *,
    origin: str,
    run_once: dict | None = None,
    worker: str = "",
    by: str = "",
) -> tuple[dict, int, bool]:
    """積む。`(積んだもの, 何番目か(1 から), 新しく積んだか)` を返す。

    **同じ組はふつう 2 度積まない**(待っているあいだに予定がまた来ても、
    走るのは 1 回でよい)。**1 回だけの上書き(`run_once`)が付いたものは別物** ——
    区画を名指しした依頼は、それぞれ違うところを見てほしいので畳まない。
    """
    if origin not in ORIGINS:
        raise ValueError(f"unknown origin: {origin}")

    def change(state: dict):
        line = state["waiting"]
        if not run_once:
            for i, entry in enumerate(line):
                if _same(entry, collection, sweep) and not entry.get("run_once"):
                    return entry, i + 1, False
        made = {
            "id": uuid.uuid4().hex[:12],
            "collection": collection,
            "sweep": sweep,
            "origin": origin,
            "at": _now(),
        }
        if run_once:
            made["run_once"] = run_once
        if worker:
            made["worker"] = worker
        if by:
            made["by"] = by
        line.append(made)
        return made, len(line), True

    return _mutate(change)


def position(entry_id: str) -> int | None:
    """待っているなら何番目か(1 から)。待っていなければ None。"""
    for i, entry in enumerate(waiting()):
        if entry.get("id") == entry_id:
            return i + 1
    return None


def take(entry_id: str) -> dict | None:
    """待っているものから外して返す。**もう無ければ None**(もう 1 本の時計が先に持っていった)。"""

    def change(state: dict):
        for i, entry in enumerate(state["waiting"]):
            if entry.get("id") == entry_id:
                return state["waiting"].pop(i)
        return None

    return _mutate(change)


def remove(entry_id: str) -> dict | None:
    """待っているものから外す(画面の「外す」)。外したものを返す。"""
    return take(entry_id)


def put_back(entry: dict) -> None:
    """起こせなかったものを**先頭へ**戻す(順番を飛ばさない)。"""

    def change(state: dict):
        if not any(e.get("id") == entry.get("id") for e in state["waiting"]):
            state["waiting"].insert(0, entry)

    _mutate(change)


def started(entry: dict, at: str | None = None) -> None:
    """起こせたものを控える。**終わったかどうかは `settle` が trigger の状態から読む**。"""

    def change(state: dict):
        state["running"].append({**entry, "started_at": at or _now()})

    _mutate(change)


def settle(status: dict | None, now: datetime | None = None) -> list[dict]:
    """控えている「走らせたもの」のうち、**もう走っていないもの**を外して返す。

    **繋がらないときは何もしない** —— 状態が読めないだけで走っていないとは
    限らない。trigger を立て直したときは走っていたものごと消えるので、
    そのときは「終わった」になる(もう一度流すのは予定とワーカーの側の仕事)。
    """
    if not status or status.get("state") == "unreachable":
        return []
    names = set(running_names(status))
    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=SETTLE_GRACE_SECONDS)

    def gone(entry: dict) -> bool:
        if entry.get("collection") in names:
            return False
        try:
            at = datetime.fromisoformat(str(entry.get("started_at") or ""))
        except ValueError:
            return True
        at = at if at.tzinfo else at.replace(tzinfo=UTC)
        return at <= cutoff

    def change(state: dict):
        done = [e for e in state["running"] if gone(e)]
        state["running"] = [e for e in state["running"] if not gone(e)]
        return done

    return _mutate(change)


def for_worker(key: str) -> bool:
    """そのワーカーのぶんが、待っているか走っているか。"""
    state = _state()
    return any(e.get("worker") == key for e in (*state["waiting"], *state["running"]))


def running_for_worker(key: str) -> bool:
    return any(e.get("worker") == key for e in running())


def drop(collection: str, sweep: str) -> None:
    """その組の**上書きの無い**待ちを外す(ほかの道で走らせたので、もう要らない)。

    **区画を名指しした依頼は残す** —— 走ったのはふつうの回で、名指ししたところを
    見たとは限らない。
    """

    def change(state: dict):
        state["waiting"] = [
            e for e in state["waiting"]
            if not (_same(e, collection, sweep) and not e.get("run_once"))
        ]

    _mutate(change)


def forget(collection: str) -> None:
    """消した収集を、待ちからも控えからも外す。"""

    def change(state: dict):
        for key in ("waiting", "running"):
            state[key] = [e for e in state[key] if e.get("collection") != collection]

    _mutate(change)
