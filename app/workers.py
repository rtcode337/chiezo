"""巡回を回す相手の順番(ワーカー)。

**「誰に頼むか」を巡回から切り離して名前で持つ。** 網羅の収集は端から端まで精査し
続けるもので、止まると困る一方、**1 つの相手の枠で回し切れるとは限らない** ——
詰まったところで止まるのではなく、次の相手へ振り替えて回り続けてほしい。

決めごと:

- **ワーカーは優先度順の並び。** 先頭から見て、**枠に余裕のある最初の相手**に頼む。
  余裕の有無は控えてある使用率で見る(`app/usage.py` の `busiest`)——
  聞きに行かない。ここは頼むたびに通る道で、そのたびに CLI を起こすわけにいかない
- **分からない相手は「余裕あり」として扱う。** 枠を出さない相手や、まだ一度も
  取れていない相手がそれで、**取れないことを理由に頼まないのでは本末転倒**になる
- **どれも詰まっていたら、その回は走らせない。** 無理に頼むと、いちばん詰まっている
  相手をさらに詰まらせる。予定も進めないので、窓が明ければ次の周で走る
  (`main._run_collections` が混んでいる trigger を避けるのと同じ形)
- **複数のワーカーを定義でき、巡回ごとにどれを使うか選べる。** 「ざっと」は速い相手、
  「調査」は賢い相手、と巡回で必要な相手が違うため
- **振り替えるのは収集だけ。** pta の売買提案は相手を振り替えない(あちらは
  「同じ材料で誰が当てるか」を測る場で、書いた相手が変わると成績の意味が変わる)。
  こちらが欲しいのは回り続けることなので、判断が逆になる

置き場は機械の側(`app/machine_store.py`)。収集の定義と同じところに並べる ——
どちらも収集の層の設定で、人が管理画面から直し、短期記憶には混ぜない。
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field

from app import machine_store, usage

log = logging.getLogger("chiezo.workers")

# 相手のセレクトで、ワーカーを名指しするときの頭。**相手 1 つと同じ欄で選ばせる**
# —— 欄を分けていた頃は、相手とワーカーの両方が選べて、どちらが効くのかが
# 画面から読めなかった(効くのはワーカーのほう)。
# **相手の id には `:` が入らない**ので、頭を見るだけで見分けられる。
OPTION_PREFIX = "worker:"

DEFS_KIND = "worker"
DEFS_KEY = "definitions"
DEFS_BROKEN = "ワーカーの定義が JSON として読めません"

# 名前に使える文字。巡回の設定から名指しするだけなので、収集の名前ほど狭くなくてよい
# (ファイル名にも URL にもならない)が、前後の空白で見分けが付かない名前は作らせない。
NAME_RE = re.compile(r"^\S(?:.{0,30}\S)?$")

# ここを超えている相手は避ける(使用率、%)。**80 は「詰まる前に譲る」ための値** ——
# 使い切ってから振り替えると、いちばん頼りたい相手の窓が明けるまで何も頼めない。
QUOTA_LIMIT = float(os.environ.get("CHIEZO_WORKER_QUOTA_LIMIT", "80") or 80)


@dataclass(frozen=True)
class Step:
    """頼む相手 1 つぶん。モデルと考える量は省ける(相手の既定に任せる)。"""

    backend: str
    model: str = ""
    effort: str = ""

    def to_json(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v}


@dataclass(frozen=True)
class Worker:
    """優先度順の相手の並び。**先頭ほど先に頼む。**"""

    name: str
    steps: tuple[Step, ...] = field(default_factory=tuple)

    def to_json(self) -> dict:
        return {"name": self.name, "steps": [s.to_json() for s in self.steps]}


def _step_from(raw) -> Step | None:
    if not isinstance(raw, dict):
        return None
    backend = str(raw.get("backend") or "").strip()
    if not backend:
        return None
    return Step(backend, str(raw.get("model") or "").strip(),
                str(raw.get("effort") or "").strip())


def _worker_from(raw) -> Worker | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not NAME_RE.match(name):
        return None
    steps = tuple(s for s in (_step_from(r) for r in raw.get("steps") or []) if s)
    return Worker(name, steps)


def load() -> list[Worker]:
    """定義の一覧(並びは配列の順)。

    **本文が壊れていたら黙って作り直さない** —— 中身ごと消えるので、読めないことを
    見せて人に直させる(収集の定義と同じ判断)。
    """
    # **置き場が無ければ「まだ無い」。** 機械の置き場を持たない面でも画面は開くので、
    # ここで断ると、ワーカーを使っていない人まで AI の面を開けなくなる
    if not machine_store.is_enabled():
        return []
    body = machine_store.get(DEFS_KIND, DEFS_KEY)
    if body is None:
        return []
    try:
        payload = json.loads(body or "{}")
        raw = payload["workers"]
        if not isinstance(raw, list):
            raise TypeError(DEFS_BROKEN)
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError(DEFS_BROKEN) from e
    return [w for w in (_worker_from(r) for r in raw) if w]


def save(items: list[Worker]) -> None:
    machine_store.put(DEFS_KIND, DEFS_KEY,
                      json.dumps({"workers": [w.to_json() for w in items]},
                                 ensure_ascii=False, indent=2))


def get(name: str) -> Worker | None:
    """名前で 1 つ。無ければ None(**読めないときも None にしない** —— 定義が
    壊れているのと、その名前が無いのは別の話なので、壊れていれば例外のまま上げる)。"""
    return next((w for w in load() if w.name == name), None)


def merged(current: list[Worker], key: str, name: str, steps: tuple[Step, ...]) -> list[Worker]:
    """1 つぶんの保存を、いまの一覧へ畳み込む。**その 1 つだけ**を書き換える。

    **名前を消すと消える**(足す口も消す口も名前 1 つ)。名前を書き換えれば改名 ——
    **巡回側の名指しは追いかけない**。名前で結んでいるので、改名したら巡回の指定も
    直すことになる(追いかけると、同じ名前の別のワーカーを作ったときに取り違える)。
    """
    if not name:
        return [w for w in current if w.name != key]
    if key and any(w.name == key for w in current):
        return [Worker(name, steps) if w.name == key else w for w in current]
    return [*current, Worker(name, steps)]


def named_in(choice: str) -> str:
    """相手のセレクトの値から、名指しされたワーカーの名前(そうでなければ空)。"""
    value = str(choice or "")
    return value[len(OPTION_PREFIX):].strip() if value.startswith(OPTION_PREFIX) else ""


def option_for(name: str) -> str:
    """その名前を相手のセレクトへ載せるときの値。"""
    return f"{OPTION_PREFIX}{name}"


def room_left(step: Step, limit: float | None = None) -> bool:
    """その相手に頼んでよいか。**控えてある使用率で見る**(聞きに行かない)。

    **分からない相手は通す。** 枠を出さない相手も、まだ一度も取れていない相手も
    ここに落ちる —— 取れないことを理由に頼まないのでは、振り替えの仕組みが
    「枠を出せる相手しか使えない」ものになってしまう。
    """
    busiest = usage.busiest(step.backend)
    return busiest is None or busiest < (QUOTA_LIMIT if limit is None else limit)


def pick(worker: Worker | None, limit: float | None = None) -> Step | None:
    """いま頼む相手。**枠に余裕のある先頭**を返す。どれも詰まっていれば None。

    **None は「待て」の意味**で、「相手がいない」ではない —— 呼ぶ側はその回を
    走らせずに見送る(予定も進めない)。窓が明ければ次の周で通る。
    """
    if worker is None:
        return None
    return next((s for s in worker.steps if room_left(s, limit)), None)
