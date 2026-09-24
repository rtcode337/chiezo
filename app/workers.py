"""巡回を回す相手の順番(ワーカー)。

**「誰に頼むか」を巡回から切り離して持つ。** 網羅の収集は端から端まで精査し
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

- **名指しは名前ではなく id で結ぶ**(`Worker.id`)。名前を鍵にしていた頃は、
  **改名した瞬間にそのワーカーを指していた巡回が行き場を失った** —— 落ちるのでは
  なく巡回自身に書いてある相手(たいてい空 = Chiezo の既定)で黙って走り続け、
  待ち行列に積んであったぶんも古い名前の下に取り残される。名前は画面に出す札で、
  人はそれを気軽に直す。**id は作ったときに決まり、二度と変わらない**

置き場は機械の側(`app/machine_store.py`)。収集の定義と同じところに並べる ——
どちらも収集の層の設定で、人が管理画面から直し、短期記憶には混ぜない。
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from app import machine_store, providers, usage

log = logging.getLogger("chiezo.workers")

# 相手のセレクトで、ワーカーを名指しするときの頭。**相手 1 つと同じ欄で選ばせる**
# —— 欄を分けていた頃は、相手とワーカーの両方が選べて、どちらが効くのかが
# 画面から読めなかった(効くのはワーカーのほう)。
# **相手の id には `:` が入らない**ので、頭を見るだけで見分けられる。
# **続くのはワーカーの id**(名前ではない。名前は変わる)。
OPTION_PREFIX = "worker:"

DEFS_KIND = "worker"
DEFS_KEY = "definitions"
DEFS_BROKEN = "ワーカーの定義が JSON として読めません"

# 名前に使える文字。**名前は画面に出す札**でしかないので、収集の名前ほど狭くなくてよい
# (ファイル名にも URL にもならない)が、前後の空白で見分けが付かない名前は作らせない。
NAME_RE = re.compile(r"^\S(?:.{0,30}\S)?$")

# 名指しの鍵。**作ったときに決まり、改名しても変わらない。**
# 巡回の `worker`・待ち行列の鍵・セレクトの値は、どれもこれを持つ。
ID_PREFIX = "w-"


def new_id() -> str:
    """新しいワーカーの id。**中身に意味を持たせない** —— 名前から作ると、
    改名したときに「元の名前」が鍵として残り続けて読む人を惑わせる。"""
    return f"{ID_PREFIX}{uuid.uuid4().hex[:8]}"

# ここを超えている相手は避ける(使用率、%)。**80 は「詰まる前に譲る」ための値** ——
# 使い切ってから振り替えると、いちばん頼りたい相手の窓が明けるまで何も頼めない。
QUOTA_LIMIT = float(os.environ.get("CHIEZO_WORKER_QUOTA_LIMIT", "80") or 80)


# ワーカーが起きる間隔と、1 度の起動で拾う数の既定。
#
# **間隔はワーカーが持ち、巡回は「積まれてよい間隔」を持つ。** 2 つは別の話 ——
# 巡回は「自分を何分おきに見てほしいか」、ワーカーは「自分が何分おきに動くか」で、
# 後者が枠の使い方を決める(積まれた数に関係なく、回るのはこの間隔)。
DEFAULT_INTERVAL_MINUTES = 60
DEFAULT_PER_RUN = 1

# 間隔の下限。収集と同じ値にしてある(あちらより短く回しても取り込みが追いつかない)。
MIN_INTERVAL_MINUTES = 5

# 1 度に拾える上限。**天井を置くのは、取り込みが 1 本ずつしか動かないから** ——
# 大きくすると、そのワーカーが長時間ぶん取り込みを占める(他のワーカーは待つ)。
MAX_PER_RUN = 20


@dataclass(frozen=True)
class Step:
    """頼む相手 1 つぶん。モデルは省ける(相手の既定に任せる)。

    **考える量は持たない** —— モデルの名前に畳んであるので(`providers.folds_effort`)、
    別に持つと食い違う組み合わせを作れてしまう。
    """

    backend: str
    model: str = ""

    def to_json(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v}


@dataclass(frozen=True)
class Worker:
    """優先度順の相手の並びと、自分の回り方。**先頭ほど先に頼む。**

    **名前と id は役割が違う。** 名前は画面に出す札で、人はいつでも直す。
    id は名指しの鍵で、作ったときに決まってからは変わらない。
    """

    name: str
    steps: tuple[Step, ...] = field(default_factory=tuple)
    # 自分が起きる間隔(分)
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES
    # 1 度の起動で待ち行列から拾う数。拾ったぶんは**1 本ずつ順に流す**
    per_run: int = DEFAULT_PER_RUN
    # 名指しの鍵。**空なら名前が鍵**(id を持たせる前に作られたもの。`key` 参照)
    id: str = ""

    @property
    def key(self) -> str:
        """名指しに使う鍵。**id を持たないものは名前で結ぶ。**

        id を足す前からある定義がそこに落ちる —— そのまま名前で結んでおけば、
        既にある巡回の名指しも待ち行列も指し先を失わない。次に保存した時点で
        その名前が id として焼き付き、以後は改名しても動かない。
        """
        return self.id or self.name

    def to_json(self) -> dict:
        return {
            "id": self.key,
            "name": self.name,
            "steps": [s.to_json() for s in self.steps],
            "interval_minutes": self.interval_minutes,
            "per_run": self.per_run,
        }


def _step_from(raw) -> Step | None:
    if not isinstance(raw, dict):
        return None
    backend = str(raw.get("backend") or "").strip()
    if not backend:
        return None
    return Step(backend, str(raw.get("model") or "").strip())


def _worker_from(raw) -> Worker | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not NAME_RE.match(name):
        return None
    steps = tuple(s for s in (_step_from(r) for r in raw.get("steps") or []) if s)
    return Worker(
        name, steps,
        interval_minutes=_positive(raw.get("interval_minutes"), DEFAULT_INTERVAL_MINUTES,
                                   MIN_INTERVAL_MINUTES),
        per_run=_positive(raw.get("per_run"), DEFAULT_PER_RUN, 1, MAX_PER_RUN),
        # **書かれていなければ名前を鍵にする**(`Worker.key`)。id を足す前の定義で、
        # そこを新しい id にすると、いま名指ししている巡回が全部行き場を失う
        id=str(raw.get("id") or "").strip(),
    )


def _positive(raw, fallback: int, low: int, high: int | None = None) -> int:
    """**読めない値は既定に落とす**(外から来る JSON なので、壊れていても止めない)。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return fallback
    value = max(value, low)
    return min(value, high) if high is not None else value


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


def get(ref: str) -> Worker | None:
    """名指しの鍵で 1 つ。無ければ None(**読めないときも None にしない** ——
    定義が壊れているのと、その鍵が無いのは別の話なので、壊れていれば例外のまま上げる)。

    **鍵で引いて、見つからなければ名前でも引く。** 拾いたいのは 2 つ ——
    id を足す前に外のアプリが控えた名指しと、人が手で書いた名前。
    **先に見るのは必ず id のほう**(同じ名前のワーカーを後から作ったときに、
    id で結んである巡回の指し先が入れ替わらないように)。
    """
    found = load()
    return (next((w for w in found if w.key == ref), None)
            or next((w for w in found if w.name == ref), None))


def label_for(ref: str) -> str:
    """その鍵のワーカーを人に見せるときの名前。**無ければ鍵をそのまま**
    (消えたワーカーを指している、が読み取れる形で出す)。

    **定義が読めなくても画面は落とさない** —— 名前は添え物で、本体は
    「ワーカーに頼む回だ」ということのほう。
    """
    try:
        found = get(ref)
    except ValueError:
        return ref
    return found.name if found else ref


def merged(
    current: list[Worker],
    key: str,
    name: str,
    steps: tuple[Step, ...],
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    per_run: int = DEFAULT_PER_RUN,
) -> list[Worker]:
    """1 つぶんの保存を、いまの一覧へ畳み込む。**その 1 つだけ**を書き換える。

    `key` は**直す相手の id**(空なら新しく足す)。**名前を消すと消える**。

    **名前を書き換えても、指し先は動かない。** 巡回の名指しも待ち行列も id で
    結んであるので、改名は画面に出る札が変わるだけ —— 名前を鍵にしていた頃は、
    直した瞬間にそのワーカーを使う巡回が黙って既定の相手で走り出していた。
    """
    if not name:
        return [w for w in current if w.key != key]
    kept = next((w for w in current if w.key == key), None) if key else None
    made = Worker(name, steps, max(interval_minutes, MIN_INTERVAL_MINUTES),
                  min(max(per_run, 1), MAX_PER_RUN),
                  id=kept.key if kept else new_id())
    if kept is not None:
        return [made if w.key == key else w for w in current]
    return [*current, made]


def ref_in(choice: str) -> str:
    """相手のセレクトの値から、名指しされたワーカーの鍵(そうでなければ空)。"""
    value = str(choice or "")
    return value[len(OPTION_PREFIX):].strip() if value.startswith(OPTION_PREFIX) else ""


def option_for(ref: str) -> str:
    """その鍵を相手のセレクトへ載せるときの値。**載せるのは id で、名前ではない**
    —— 名前を載せると、改名したときに保存済みの巡回が指し先を失う。"""
    return f"{OPTION_PREFIX}{ref}"


# ---- 待ち行列 ---------------------------------------------------------------
#
# **定義とは別の置き場に持つ。** 定義は人が画面のフォームから丸ごと送り直すので、
# 同じ控えに入れておくと**編集のたびに行列が消える**(押した人には何も起きていない
# ように見えて、積んであったものだけが失われる)。
#
# 形は `{"<ワーカーの id>": {"queue": [...], "batch": [...], "next_run_at": "..."}}`。
# **鍵は id。** 名前で分けていた頃は、改名した拍子に積んであったぶんが古い名前の
# 下へ取り残され、誰も流さないまま残った。
# `queue` が待っているもの、`batch` が**いま流している最中のぶん**(起動で拾った塊)。
# 塊を分けて持つのは、**流し切るまでそのワーカーに優先権を持たせる**ため ——
# 途中で他のワーカーに割り込まれると、「1 度の起動で N 本」が意味を失う。

QUEUE_KEY = "queue"


def _queue_all() -> dict:
    if not machine_store.is_enabled():
        return {}
    body = machine_store.get(DEFS_KIND, QUEUE_KEY)
    if not body:
        return {}
    try:
        found = json.loads(body)
    except ValueError:
        # **読めなければ空から始める。** 行列は作り直せる(次の周で積み直る)ので、
        # 定義と違って人に直させる価値が無い
        log.warning("worker queue is unreadable; starting over")
        return {}
    return found if isinstance(found, dict) else {}


def _queue_save(state: dict) -> None:
    if machine_store.is_enabled():
        machine_store.put(DEFS_KIND, QUEUE_KEY,
                          json.dumps(state, ensure_ascii=False, indent=2))


def _slot(state: dict, ref: str) -> dict:
    slot = state.setdefault(ref, {})
    slot.setdefault("queue", [])
    slot.setdefault("batch", [])
    return slot


def _entry(collection: str, sweep: str) -> dict:
    return {"collection": collection, "sweep": sweep}


def _same(a: dict, b: dict) -> bool:
    return a.get("collection") == b.get("collection") and a.get("sweep") == b.get("sweep")


def queued(ref: str) -> list[dict]:
    """そのワーカーが待っているもの(流している最中のぶんを先頭に)。"""
    slot = _slot(_queue_all(), ref)
    return [*slot["batch"], *slot["queue"]]


def enqueue(ref: str, collection: str, sweep: str, at: str) -> bool:
    """待ち行列へ積む。**既に居れば積まない**(二重に走らせないため)。積んだら True。"""
    state = _queue_all()
    slot = _slot(state, ref)
    made = _entry(collection, sweep)
    if any(_same(made, e) for e in (*slot["queue"], *slot["batch"])):
        return False
    slot["queue"].append({**made, "at": at})
    _queue_save(state)
    return True


def claim(ref: str, per_run: int, at: str) -> list[dict]:
    """起動 1 回ぶんを行列から塊へ移す。**既に流している最中なら何もしない**。

    拾うのは先頭から `per_run` 件。**空振りでも起動したことにする**(次の起動まで
    間隔を空ける)—— 積まれていないワーカーが毎周見に来ても、することは無い。
    """
    state = _queue_all()
    slot = _slot(state, ref)
    if slot["batch"]:
        return list(slot["batch"])
    slot["batch"] = slot["queue"][:max(1, per_run)]
    slot["queue"] = slot["queue"][len(slot["batch"]):]
    slot["last_run_at"] = at
    _queue_save(state)
    return list(slot["batch"])


def claim_ready(ref: str) -> bool:
    """いま流している最中の塊があるか(あれば拾い直さない)。"""
    return bool(_slot(_queue_all(), ref)["batch"])


def last_at(ref: str) -> str:
    """そのワーカーが最後に起動した時刻(まだなら空)。

    **起動であって、流し終えた時刻ではない。** 次にいつ起きるかはここから数えるので、
    塊を流し切るのに何周かかっても、次の起動は最初の起動から間隔ぶん後になる。
    """
    slot = _slot(_queue_all(), ref)
    # 前の版は同じ値を `next_run_at` に入れていた(名前が逆だった)
    return str(slot.get("last_run_at") or slot.get("next_run_at") or "")


def done(ref: str, collection: str, sweep: str) -> None:
    """流し終えた 1 件を外す。**塊からも行列からも**。

    **行列のほうも外すのは、待っているあいだに手で走らされることがあるから**
    (画面の「今すぐ実行」)。残しておくと、そのワーカーが起きたときにもう一度
    同じ回が流れる —— 枠を 1 回ぶん余計に食う。
    """
    state = _queue_all()
    slot = _slot(state, ref)
    made = _entry(collection, sweep)
    for key in ("queue", "batch"):
        slot[key] = [e for e in slot[key] if not _same(made, e)]
    _queue_save(state)


def drop(collection: str, sweep: str) -> None:
    """その回を、**全部のワーカーの行列と塊から**外す。

    **走らせたのだから、もうどこにも積まれていてはいけない。** 外さないと、
    そのワーカーが起きたときに同じ回がもう一度流れる —— 枠を 1 回ぶん余計に食う。

    **1 つのワーカーだけを見ては足りない**(`done` との違いがここ)。積んだ後に
    巡回のワーカーを付け替えれば、積まれているのは**前のワーカー**の行列だし、
    試し撃ちで相手を上書きした回は、上書きした先のワーカーを見ても居ない ——
    どちらも「外したつもり」で残る。**誰の行列に居るかを当てに行かない**。

    `done` のほうは流し終えた 1 本を、そのワーカーの塊から外すためのもので
    残す(あちらは「誰が流したか」が分かっている)。
    """
    if not collection or not sweep:
        return
    made = _entry(collection, sweep)
    state = _queue_all()
    for slot in state.values():
        for key in ("queue", "batch"):
            slot[key] = [e for e in slot.get(key) or [] if not _same(made, e)]
    _queue_save(state)


def forget(collection: str) -> None:
    """その収集のぶんを全部のワーカーから外す(収集を消したとき)。"""
    state = _queue_all()
    for slot in state.values():
        for key in ("queue", "batch"):
            slot[key] = [e for e in slot.get(key) or [] if e.get("collection") != collection]
    _queue_save(state)


FULL_KEY = "full"

# 相手が「枠を使い切った」と言ったのに、いつ明けるかを言わなかったときの待ち時間。
# **短くしすぎない** —— 明ける前に頼み直すと、そのたびに 1 回ぶん無駄に叩く。
DEFAULT_COOLDOWN_MINUTES = 60

# 相手の言い分から「枠切れ」を読み取る手掛かり。**控えの使用率より新しい報せ** ——
# 使用率は定時にしか採らないので、長い 1 回の途中で窓が閉まると次の採取まで
# 気づけない(本番で 41% のまま 89% まで走り続けた)。
_FULL_PHRASES = ("quota", "rate limit", "usage limit", "out of credit", "resource_exhausted")

# 「あと 1h15m20s で明ける」の読み取り。相手の文面は英語で来る
_RESETS_RE = re.compile(
    r"resets?\s+in\s+(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?", re.I
)


def looks_full(reason: str) -> bool:
    """相手の言い分が「枠を使い切った」か。"""
    low = str(reason or "").lower()
    return any(word in low for word in _FULL_PHRASES)


def cooldown_minutes(reason: str) -> int:
    """その言い分から、何分のあいだ避ければよいか。**読めなければ既定**。"""
    found = _RESETS_RE.search(str(reason or ""))
    if not found or not any(found.groups()):
        return DEFAULT_COOLDOWN_MINUTES
    hours, minutes, seconds = (int(g or 0) for g in found.groups())
    # 端数は切り上げる。**明ける直前に頼み直すと、また断られて 1 回ぶん損をする**
    return max(1, hours * 60 + minutes + (1 if seconds else 0))


def avoid_for_now(backend: str, reason: str, model: str = "") -> str:
    """相手が「使い切った」と言ったので、明けるまで避ける。避ける期限を返す。

    **言い分に明ける時刻が入っていればそれを使う**(「Resets in 1h15m20s」)。
    無ければ既定の待ち時間 —— どちらにしても、次の 1 回は待たずに次の段へ回る。

    **避けるのは、そのモデルが食う枠だけ**(`model`)。1 人の相手が独立した枠を
    何本も持つことがあるので、相手ごと避けると別の枠に置いた段まで巻き添えになる。
    """
    until = (
        datetime.now(UTC) + timedelta(minutes=cooldown_minutes(reason))
    ).isoformat(timespec="seconds")
    mark_full(backend, until, model)
    return until


def _full_key(backend: str, model: str = "") -> str:
    """締め出しの印を置く鍵。**枠を分けられる相手では枠ごと**。

    分けられないとき(枠が 1 本の相手・モデルを書いていない段)は相手の名前そのまま
    —— それは「この相手ぜんぶ」の意味になる。
    """
    group = providers.quota_group(backend, model)
    return f"{backend}|{group}" if group else backend


def mark_full(backend: str, until: str, model: str = "") -> None:
    """その相手(の枠)を、**いつまで避けるか**を控える。

    **相手が言ったことのほうが新しい。** 控えてある使用率は定時にしか採らないので、
    1 回が長い回の途中で窓が閉まっても次の採取まで気づけない —— 実際に、41% と
    控えたまま同じ相手へ 4 回続けて投げ、最後に断られた。
    断られた事実をここへ書けば、次の 1 回は待たずに次の段へ回る。
    """
    if not backend or not machine_store.is_enabled():
        return
    key = _full_key(backend, model)
    state = _queue_all()
    state.setdefault(FULL_KEY, {})[key] = until
    _queue_save(state)
    log.info("avoiding %s until %s (the provider said it is full)", key, until)


def full_until(backend: str, model: str = "") -> str:
    """その段を避ける期限(空なら避けていない)。

    **相手の名前だけの印も必ず見る。** あれは「どの枠か分からないまま断られた」
    ぶんで、**その相手ぜんぶに効く** —— 枠ごとの印しか見ないと、素通りしてしまう
    (枠を分ける前に置かれた古い印もここに入る)。**遅いほうを採る**(慎重な側)。
    """
    found = _queue_all().get(FULL_KEY)
    if not isinstance(found, dict):
        return ""
    keys = {backend, _full_key(backend, model)}
    return max((str(found.get(k) or "") for k in keys), default="")


def room_left(step: Step, limit: float | None = None, now: str = "") -> bool:
    """その相手に頼んでよいか。**控えてある使用率で見る**(聞きに行かない)。

    **分からない相手は通す。** 枠を出さない相手も、まだ一度も取れていない相手も
    ここに落ちる —— 取れないことを理由に頼まないのでは、振り替えの仕組みが
    「枠を出せる相手しか使えない」ものになってしまう。

    **相手が「使い切った」と言ったぶんは、期限まで避ける**(`mark_full`)。
    使用率より新しい報せなので、こちらを先に見る。

    **見るのは、その段のモデルが食う枠だけ**(`usage.busiest` にモデルを渡す)。
    1 人の相手が独立した枠を何本も持つことがあり、まとめて見ると**片方が
    詰まっただけで、同じ相手の別の枠に置いた段まで飛ばされる** —— 逃げ先の
    ために並べた段が、いちばん要るときに働かない。
    """
    if (until := full_until(step.backend, step.model)) and (now or _now_iso()) < until:
        return False
    busiest = usage.busiest(step.backend, step.model)
    return busiest is None or busiest < (QUOTA_LIMIT if limit is None else limit)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def pick(worker: Worker | None, limit: float | None = None, now: str = "") -> Step | None:
    """いま頼む相手。**枠に余裕のある先頭**を返す。どれも詰まっていれば None。

    **None は「待て」の意味**で、「相手がいない」ではない —— 呼ぶ側はその回を
    走らせずに見送る(予定も進めない)。窓が明ければ次の周で通る。

    **区画ごとに呼び直される。** 1 回で何区画も回る収集があるので、回の頭で
    1 度だけ決めると、途中で窓が閉まっても同じ相手に投げ続けることになる。
    """
    if worker is None:
        return None
    return next((s for s in worker.steps if room_left(s, limit, now)), None)
