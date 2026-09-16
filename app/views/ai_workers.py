"""管理画面の「ワーカー」節 —— 巡回を回す相手の順番(`app/workers.py`)。

**使用量のすぐ下に置く。** 何を見て振り替えているかがその表なので、離すと
「なぜこの相手に回ったのか」を別の画面と突き合わせて読むことになる。

**名前を書けば増え、消せば減る**(巡回と同じ流儀)。行ごとにボタンを付けると、
押した先で何が起きるかを別に説明することになる。

**段も同じ。** 相手を選べば段が増え、「(選ばない)」に戻せば減る —— 空の段が
1 つ常に出ているので、足すのに押す手数が要らない。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app import usage, workers
from app.pages import esc

router = APIRouter()

SECTION_ANCHOR = "ai-workers"
BACK_TO_SECTION = f"/admin/ai#{SECTION_ANCHOR}"

# 1 つのワーカーに出す段の数(いま入っているぶん + 足すための空き 1 つ)。
# **上限を持つのは画面の都合だけ** —— 並びは配列なので、定義側に制限は無い。
MAX_STEPS = 6


def _percent(provider: str) -> str:
    """いまの詰まり具合。**避ける相手が一目で分かるように、しきい値と並べて出す。**"""
    busiest = usage.busiest(provider)
    if busiest is None:
        return '<span class="muted">枠は出せない</span>'
    mark = " ⚠️" if busiest >= workers.QUOTA_LIMIT else ""
    return f'<span class="muted">{busiest:.0f}% 使用{mark}</span>'


def _step_row(index: int, step: workers.Step | None, backend_select, model_select,
              effort_select) -> str:
    current = step.backend if step else ""
    return (
        '<div class="sweep-row">'
        f'<p><label>{index + 1} 番目<br>{backend_select(current, "step_backend")}</label>'
        f" {_percent(current) if current else ''}</p>"
        f'<p><label>モデル<br>{model_select(current, step.model if step else "", "step_model")}'
        "</label></p>"
        f'<p><label>考える量<br>'
        f'{effort_select(current, step.effort if step else "", "step_effort")}</label></p>'
        "</div>"
    )


def _worker_form(worker: workers.Worker | None, selects) -> str:
    backend_select, model_select, effort_select = selects
    name = worker.name if worker else ""
    steps = list(worker.steps) if worker else []
    # **空の段を 1 つ足して出す。** 足すのに押す手数を要らなくするため
    rows = [
        _step_row(i, steps[i] if i < len(steps) else None,
                  backend_select, model_select, effort_select)
        for i in range(min(len(steps) + 1, MAX_STEPS))
    ]
    hint = ("名前を消すと、このワーカーは無くなります" if worker
            else "名前を書くと増えます")
    return (
        f'<form method="post" action="/admin/ai/workers" class="collect-form">'
        f'<input type="hidden" name="worker_key" value="{esc(name)}">'
        f'<p><label>名前<br><input name="worker_name" value="{esc(name)}"'
        f' placeholder="精査"></label> <span class="muted">{esc(hint)}</span></p>'
        f"{''.join(rows)}"
        '<p><button type="submit">このワーカーを保存</button></p></form>'
    )


def section_html(selects) -> str:
    """節ぜんたい。`selects` は相手・モデル・考える量のセレクトを作る 3 つ。

    **管理画面の部品を借りる**(`views/admin.py`)—— 同じ選び方を 2 か所に書くと、
    有効な相手の数え方や「既定にまかせる」の扱いが画面ごとにずれる。
    """
    try:
        items = workers.load()
        broken = ""
    except ValueError as e:
        items, broken = [], str(e)
    forms = "".join(
        f'<details><summary>{esc(w.name)}({len(w.steps)} 段)</summary>'
        f"{_worker_form(w, selects)}</details>"
        for w in items
    )
    add = f'<details><summary>ワーカーを足す</summary>{_worker_form(None, selects)}</details>'
    note = (f'<p class="stale">⚠️ {esc(broken)}</p>' if broken else "")
    if not items and not broken:
        note = ('<p class="muted">まだありません。'
                "巡回から名指しすると、その並びで相手を選ぶようになります。</p>")
    return f"""<h3 id="{SECTION_ANCHOR}">ワーカー</h3>
<details>
<summary>この節について</summary>
<p><strong>巡回を回す相手の順番。</strong>網羅の収集は端から端まで精査し続けるもので、
<strong>1 つの相手の枠で回し切れるとは限らない</strong> —— 詰まったところで止まるのではなく、
次の相手へ振り替えて回り続けるための並び。</p>
<p><strong>先頭から見て、枠に余裕のある最初の相手に頼む。</strong>
使用率が <strong>{workers.QUOTA_LIMIT:.0f}%</strong> を超えている相手は避ける
(<code>CHIEZO_WORKER_QUOTA_LIMIT</code> で変えられる)。判断に使うのは上の表と同じ控えで、
<strong>選ぶたびに相手へ問い合わせない</strong>。</p>
<p><strong>枠を出さない相手は締め出さない。</strong>まだ一度も取れていない相手も同じ扱いで、
「分からない」を理由に外すと、いちばん頼りたい相手が使えなくなる。</p>
<p><strong>どれも詰まっていたら、その回は走らせない。</strong>予定も進めないので、
窓が明けば次の周で走る。<strong>巡回に書いてある相手へは落ちない</strong>
—— 落ちると、避けたかった相手に頼むことがある。</p>
</details>
{note}
{forms}
{add}
"""


@router.post("/admin/ai/workers")
async def save_worker(request: Request):
    """ワーカー 1 つぶんを保存する。**その 1 つだけ**を書き換える。

    **名前を消すと、そのワーカーが消える**(足す口も消す口も名前 1 つ)。
    名前を書き換えれば改名になる —— 巡回側の名指しは追いかけない(名前で結んで
    いるので、改名したら巡回の指定も直すことになる)。
    """
    form = await request.form()
    key = str(form.get("worker_key") or "").strip()
    name = str(form.get("worker_name") or "").strip()
    backends = form.getlist("step_backend")
    models = form.getlist("step_model")
    efforts = form.getlist("step_effort")
    steps = tuple(
        workers.Step(str(b).strip(), str(models[i] if i < len(models) else "").strip(),
                     str(efforts[i] if i < len(efforts) else "").strip())
        for i, b in enumerate(backends) if str(b).strip()
    )

    try:
        current = workers.load()
    except ValueError:
        # **壊れた定義を黙って捨てない。** 直しに来た人の 1 つが、残りを消す操作に
        # なってはいけない
        return RedirectResponse(url=BACK_TO_SECTION, status_code=303)

    workers.save(workers.merged(current, key, name, steps))
    return RedirectResponse(url=BACK_TO_SECTION, status_code=303)
