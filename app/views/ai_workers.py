"""管理画面の「ワーカー」節 —— 巡回を回す相手の順番(`app/workers.py`)。

**使用量のすぐ下に置く。** 何を見て振り替えているかがその表なので、離すと
「なぜこの相手に回ったのか」を別の画面と突き合わせて読むことになる。

**名前を書けば増え、消せば減る**(巡回と同じ流儀)。行ごとにボタンを付けると、
押した先で何が起きるかを別に説明することになる。

**段も同じ。** 相手を選べば段が増え、「(選ばない)」に戻せば減る —— 空の段が
1 つ常に出ているので、足すのに押す手数が要らない。
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app import jst, usage, workers
from app.pages import esc
from app.views.ai_usage import percent_text

router = APIRouter()

SECTION_ANCHOR = "ai-workers"
BACK_TO_SECTION = f"/admin/collect#{SECTION_ANCHOR}"

# 保存のボタンの文言。**2 か所で使う** —— 目に見えるほうと、エンターの送り先に
# する見えないほう(`_default_submit`)。書き分けると、片方だけ直したときに
# 「押したボタン」と「エンターで起きること」が食い違って見える。
SAVE_LABEL = "このワーカーを保存"

# 1 つのワーカーに出す段の数(いま入っているぶん + 足すための空き 1 つ)。
# **上限を持つのは画面の都合だけ** —— 並びは配列なので、定義側に制限は無い。
MAX_STEPS = 6

# 段の相手が空のときの出し方。**「Chiezo の既定にまかせる」とは書かない** ——
# 足していない段にそれが出ていると、生きているのか未設定なのかが読めない。
_EMPTY_STEP = "(使わない)"


def _percent(provider: str, model: str = "") -> str:
    """いまの詰まり具合。**避ける相手が一目で分かるように、しきい値と並べて出す。**

    **その段のモデルが食う枠で見る**(`usage.busiest` にモデルを渡す)——
    1 人の相手が独立した枠を何本も持つことがあり、渡さないと全窓の最大が出る。
    Antigravity は Gemini と Claude/GPT が別勘定なので、**Gemini の段に
    Claude 枠の数字が出ていた** —— 判断する側(`workers.room_left`)は枠ごとに
    見ているのに、**画面だけが混ぜたまま**だった。数字と振る舞いが食い違うと、
    避けられていない段が「詰まっている」に見える(逆も起きる)。
    """
    busiest = usage.busiest(provider, model)
    if busiest is None:
        return '<span class="muted">枠は出せない</span>'
    mark = " ⚠️" if busiest >= workers.QUOTA_LIMIT else ""
    return f'<span class="muted">{percent_text(busiest)}% 使用{mark}</span>'


def _step_row(
    index: int, step: workers.Step | None, backend_select, model_select, last: int = -1
) -> str:
    """段 1 つぶんの欄。

    **考える量の欄は持たない。** 考える量はモデルの名前に畳んである
    (`sonnet-high` / `gpt-6-astra-max`)ので、別の欄を残すと**選べるのに効かない欄**
    になる(`providers.drops_effort`)。

    **空の段は「使わない」を選んだ状態で出す。** 相手の欄の既定は「Chiezo の既定に
    まかせる」だが、**足していない段にそれが出ていると、生きているのか未設定なのかが
    読めない** —— 段は書いた順に試す並びなので、空欄は「ここで終わり」を意味する。
    """
    current = step.backend if step else ""
    # **その段のモデルまで渡す。** 相手だけで引くと、枠を何本も持つ相手で
    # 別の枠の数字が出る(判断する側は枠ごとに見ているので食い違う)
    picked = (
        _percent(current, step.model if step else "")
        if current else '<span class="muted">(使わない)</span>'
    )
    return (
        '<div class="sweep-row">'
        f'<p><label>{index + 1} 番目<br>'
        f'{backend_select(current, "step_backend", empty_label=_EMPTY_STEP)}</label>'
        f" {picked} {_move_step_html(index, step, last)}</p>"
        f'<p><label>モデル<br>{model_select(current, step.model if step else "", "step_model")}'
        "</label></p>"
        "</div>"
    )


def _move_step_html(index: int, step: workers.Step | None, last: int) -> str:
    """段を 1 つ上/下へ動かす印。**段の並びは「詰まったら次へ」の順そのもの**。

    **入れ替えるのに打ち直させない。** 並びを変えるには相手を選び直すしかなく、
    3 段あれば 3 つとも選び直すことになっていた —— そのあいだに 1 つ間違えると、
    無人で回る層が別の相手に回り続ける。

    **このフォームの submit として出す**(別のフォームにしない)。`<form>` は
    入れ子にできないので、中にもう 1 枚置くと**内側が丸ごと無視される**
    (押しても何も起きないボタンになる)。同じフォームなら、**書きかけの欄も
    一緒に保存してから動く** —— 動かすために保存し直す手間も要らない。

    **そのぶん、エンターの送り先を奪わないようにする**(`_default_submit`)。
    欄で改行キーを押したときに送られるのは**並びの中でいちばん先にある submit**
    なので、何もしないとここが選ばれる —— 名前を直してエンターを押しただけで、
    保存と一緒に段が 1 つ入れ替わっていた(押した人には名前を変えただけに見える)。

    **端では出さない**(押せないボタンを置かない、の流儀)。空の段(まだ相手を
    選んでいない末尾の 1 行)にも出さない —— 動かす中身が無い。
    """
    if step is None:
        return ""
    up = (
        "" if index == 0 else
        '<button type="submit" name="step_move" class="step-move"'
        f' value="up:{index}" title="上へ">↑</button>'
    )
    down = (
        "" if index >= last else
        '<button type="submit" name="step_move" class="step-move"'
        f' value="down:{index}" title="下へ">↓</button>'
    )
    return up + down


def _default_submit(label: str) -> str:
    """欄でエンターを押したときに送られるボタン。**目には出さないが、先頭に置く。**

    ブラウザは**並びの中でいちばん先にある submit** を送る。このフォームには
    段を動かす ↑↓ が混ざっていて、置かないとそちらが選ばれる ——
    **名前を直してエンターを押しただけで、AI の並びが 1 つ入れ替わる**
    (押した人には名前を変えただけに見えるので、後から気づけない)。

    **`display: none` にはしない**(送り先として扱わないブラウザがある)。
    タブでは止まらないようにして、読み上げにも出さない —— 下にある
    「保存」と同じ働きのものが 2 つ読み上げられても混乱するだけなので。
    """
    return (f'<button type="submit" class="default-submit" tabindex="-1"'
            f' aria-hidden="true">{esc(label)}</button>')


def _worker_form(worker: workers.Worker | None, selects, running: str = "") -> str:
    backend_select, model_select = selects
    name = worker.name if worker else ""
    # **直す相手は id で指す。** 名前で指していた頃は、名前を書き換えた保存が
    # 「別のワーカーを足す」になりかねず、指し先も巡回から外れた
    key = worker.key if worker else ""
    steps = list(worker.steps) if worker else []
    # **空の段を 1 つ足して出す。** 足すのに押す手数を要らなくするため
    rows = [
        _step_row(
            i, steps[i] if i < len(steps) else None, backend_select, model_select,
            last=len(steps) - 1,
        )
        for i in range(min(len(steps) + 1, MAX_STEPS))
    ]
    hint = ("名前を消すと、このワーカーは無くなります" if worker
            else "名前を書くと増えます")
    every = worker.interval_minutes if worker else workers.DEFAULT_INTERVAL_MINUTES
    take = worker.per_run if worker else workers.DEFAULT_PER_RUN
    return (
        f'<form method="post" action="/admin/ai/workers" class="collect-form">'
        # **エンターでは保存する**(段の ↑↓ に送り先を奪わせない)
        f'{_default_submit(SAVE_LABEL)}'
        f'<input type="hidden" name="worker_key" value="{esc(key)}">'
        f'<p><label>名前<br><input name="worker_name" value="{esc(name)}"'
        f' placeholder="精査"></label> <span class="muted">{esc(hint)}</span></p>'
        f'<p><label>流し終えてから次に起きるまでの間(分。{workers.MIN_INTERVAL_MINUTES} 以上)<br>'
        f'<input name="worker_interval" type="number"'
        f' min="{workers.MIN_INTERVAL_MINUTES}" value="{every}"></label></p>'
        f'<p><label>1 度に拾う数(1〜{workers.MAX_PER_RUN})<br>'
        f'<input name="worker_per_run" type="number" min="1"'
        f' max="{workers.MAX_PER_RUN}" value="{take}"></label></p>'
        '<p class="muted">拾ったぶんは<strong>1 本ずつ順に流します</strong>'
        "(同じ相手へ同時に 2 本投げないため。取り込みを並べられる設定でも、"
        "1 つのワーカーから出るのは 1 本ずつ)。流し切ったら、上の間だけ休んでから"
        "次を拾います。</p>"
        f"{''.join(rows)}"
        f'<p><button type="submit">{esc(SAVE_LABEL)}</button></p></form>'
        + (_queue_html(worker, running) if worker else "")
    )


def _number(raw, fallback: int) -> int:
    """フォームの数。**読めない値は既定に落とす**(画面は JS を持たないので、
    人が URL を手で書き換えることがある)。"""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback


def _queue_html(worker: workers.Worker, running: str = "") -> str:
    """そのワーカーの回り方と、待っているもの。

    **出すのは「なぜ動いていないか」を読むため。** 積まれているのに動かないなら
    枠が詰まっているか起動の間隔待ちで、積まれていないなら巡回の間隔待ち ——
    どちらなのかは、時刻と行列を並べないと外から判らない。

    **畳まない。** 巡回の表と同じで、動いているかを確かめに来る場所なので、
    開かないと読めないのでは表の値打ちが消える。
    """
    woke = jst.parse(workers.last_at(worker.key))
    ended = jst.parse(workers.finished_at(worker.key))
    flowing = workers.claim_ready(worker.key)
    when_woke = esc(jst.format(woke)) if woke else '<span class="muted">まだ</span>'
    # **流している最中は「終わった」を出さない**(前の塊の時刻が今の回のものに見える)
    if flowing or (woke and (not ended or ended < woke)):
        when_ended = '<span class="muted">流している最中</span>'
        when_next = '<span class="muted">流し終えてから</span>'
    elif ended is None and woke is None:
        when_ended = '<span class="muted">まだ</span>'
        when_next = '<span class="muted">いますぐ</span>'
    else:
        last = ended or woke
        when_ended = esc(jst.format(last))
        when_next = esc(jst.format(last + timedelta(minutes=worker.interval_minutes)))
    waiting = workers.queued(worker.key)
    if not waiting:
        rows = '<tr><td colspan="2" class="muted">待っているものはありません</td></tr>'
    else:
        rows = "".join(
            f"<tr><td>{esc(str(e.get('collection') or ''))}"
            f" / {esc(str(e.get('sweep') or ''))}</td>"
            + (f'<td>{"いま流している" if flowing and i == 0 else ""}</td></tr>')
            for i, e in enumerate(waiting)
        )
    return f"""
<table>
<thead><tr><th>前回起きた</th><th>前回流し終えた</th><th>次に起きる</th><th>待っている数</th></tr></thead>
<tbody><tr><td>{when_woke}</td><td>{when_ended}</td><td>{when_next}</td><td>{len(waiting):,}</td></tr></tbody>
</table>
<table>
<thead><tr><th>待ち行列(先に積まれた順)</th><th></th></tr></thead>
<tbody>{rows}</tbody>
</table>
{_wake_form(worker, bool(waiting), running)}
<p class="muted">
<strong>次に起きる時刻は、前回「流し終えた」時刻から数えます</strong>(起きた時刻では
ない)—— 塊を流すのに間隔より長くかかっても、流し終えたあと必ず間隔ぶん休む。<br>
積まれているのに動かないなら、枠が詰まっているか起動待ち。積まれていないなら、
巡回の側がまだ積んでいない(前回の完了から間隔が空いていない)。
</p>
"""


def _wake_form(worker: workers.Worker, waiting: bool, running: str = "") -> str:
    """時計を待たずに 1 本流す口。**待っているものが無ければ出さない**。

    **枠が明いているうちに回しておきたい、が普通に起きる** —— 次の起動まで待つと、
    待っているあいだに他の依頼が枠を食う(実測で、外からの 1 回が 5 時間枠を
    48 ポイント持っていった)。押せば行列の先頭が 1 本流れ、**そこから間隔を
    数え直す**(起こしたことになるので、次の起動は押した時刻からずれる)。

    **取り込みが埋まっていても押せる。** 取り込みの待ち行列に並び、空いた周で
    流れる(`running` は、そのとき走っているものを添えるためだけに使う)。
    """
    if not waiting:
        return ('<p class="muted">待っているものが無いので、起こしても流すものが'
                "ありません。</p>")
    note = (
        f'<span class="muted">いま取り込みが走っています({esc(running)})。'
        "押すと取り込みの待ち行列に並び、空いたら流れます。</span>"
        if running else ""
    )
    return note + (
        f'<form class="init-form" method="post" action="/admin/ai/workers/wake">'
        f'<input type="hidden" name="worker_id" value="{esc(worker.key)}">'
        f'<button type="submit"'
        f' title="間隔を待たずに、待ち行列の先頭を 1 本流します">今すぐ起こす</button>'
        "</form>"
    )


def section_html(selects, running: str = "") -> str:
    """節ぜんたい。`selects` は相手とモデルのセレクトを作る 2 つ。

    `running` は**いま取り込みが走っている相手**(空なら走っていない)。
    呼ぶ側が持っているものを渡す —— ここで聞き直すと、1 回の描画で trigger を
    何度も叩くことになる。

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
        f"{_worker_form(w, selects, running)}</details>"
        for w in items
    )
    add = f'<details><summary>ワーカーを足す</summary>{_worker_form(None, selects)}</details>'
    note = (f'<p class="stale">⚠️ {esc(broken)}</p>' if broken else "")
    if not items and not broken:
        note = ('<p class="muted">まだありません。'
                "巡回から名指しすると、その並びで相手を選ぶようになります。</p>")
    return f"""<h2 id="{SECTION_ANCHOR}">ワーカー(巡回を回す相手)</h2>
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


@router.post("/admin/ai/workers/wake")
async def wake_worker(request: Request):
    """ワーカーを**時計を待たずに起こす**(行列の先頭を 1 本流す)。

    枠が明いているうちに回しておきたい、が普通に起きる —— 次の起動まで待つと、
    待っているあいだに他の依頼が枠を食う。

    **断る理由は書き分ける**(`main.wake_worker`)。行列が空なのか枠が詰まって
    いるのかで、次にすることが逆になる(積むのを待つ / 窓が明くのを待つ)。
    """
    from app.main import wake_worker as wake

    form = await request.form()
    wake(str(form.get("worker_id") or "").strip())
    return RedirectResponse(BACK_TO_SECTION, status_code=303)


def _moved_steps(steps: tuple, raw: str) -> tuple:
    """`up:1` / `down:0` で段を 1 つ動かした並び。読めない指示は無視する。

    **端は動かさない**(画面は端にボタンを出さないが、押された形は入ってくる)。
    """
    kind, _, index = raw.partition(":")
    if kind not in ("up", "down") or not index.isdigit():
        return steps
    at = int(index)
    to = at - 1 if kind == "up" else at + 1
    if not (0 <= at < len(steps) and 0 <= to < len(steps)):
        return steps
    out = list(steps)
    out[at], out[to] = out[to], out[at]
    return tuple(out)


@router.post("/admin/ai/workers")
async def save_worker(request: Request):
    """ワーカー 1 つぶんを保存する。**その 1 つだけ**を書き換える。

    **どれを直すかは id で決まる**(`worker_key`。空なら新しく足す)。
    **名前を消すと、そのワーカーが消える。** 名前を書き換えれば改名で、
    **巡回の名指しも待ち行列もそのまま**動く —— 結んでいるのは id のほう。
    """
    form = await request.form()
    key = str(form.get("worker_key") or "").strip()
    name = str(form.get("worker_name") or "").strip()
    backends = form.getlist("step_backend")
    models = form.getlist("step_model")
    every = _number(form.get("worker_interval"), workers.DEFAULT_INTERVAL_MINUTES)
    take = _number(form.get("worker_per_run"), workers.DEFAULT_PER_RUN)
    # **考える量は受け取らない。** モデルの名前に畳んであるので、別に持つと
    # 食い違う組み合わせを作れてしまう(`providers.drops_effort`)
    steps = tuple(
        workers.Step(str(b).strip(), str(models[i] if i < len(models) else "").strip())
        for i, b in enumerate(backends) if str(b).strip()
    )
    # **↑↓ はこのフォームの submit。** 書きかけの欄も一緒に保存してから動かす
    # (`_move_step_html`。`<form>` は入れ子にできないので、別フォームにはできない)
    steps = _moved_steps(steps, str(form.get("step_move") or ""))

    try:
        current = workers.load()
    except ValueError:
        # **壊れた定義を黙って捨てない。** 直しに来た人の 1 つが、残りを消す操作に
        # なってはいけない
        return RedirectResponse(url=BACK_TO_SECTION, status_code=303)

    workers.save(workers.merged(current, key, name, steps, every, take))
    return RedirectResponse(url=BACK_TO_SECTION, status_code=303)
