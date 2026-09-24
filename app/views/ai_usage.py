"""管理画面の「使用量」節 —— 各 AI の枠(残り)と、Chiezo が使ったぶん。

「AI の相手」の表とは分けてある。 あちらは設定を一度入れたら開かない場所、
こちらは何度も見に来る場所で、読む目的が違う(そして相手を選ぶための情報でもある ——
枠を使い切っている相手には重い仕事を頼まない)。

描画のときに相手へ問い合わせない(「接続を試す」と同じ流儀)。控えてある値と
「いつ取ったか」を出し、取り直しは行ごとのボタンで。落ちている相手がいても画面は遅れない。
"""
from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import ai_inflight, jst, usage, usage_store
from app.pages import esc
from app.views import ai_history

router = APIRouter()

SECTION_ANCHOR = "ai-usage"
BACK_TO_SECTION = f"/admin/ai#{SECTION_ANCHOR}"

# Chiezo が使ったぶんを出す窓の見出し(`usage.SPENT_WINDOWS` と同じ並び)。
_SPENT_LABELS = {"5h": "直近 5 時間", "24h": "直近 24 時間", "7d": "直近 7 日"}


def _when(raw: str) -> str:
    """控えの時刻を JST の 1 行に。読めない値は空(画面を落とさない)。"""
    when = jst.parse(raw)
    return jst.format(when) if when else ""


def _meter(percent: float | None) -> str:
    """使用率の帯。数字も必ず添える —— 帯だけだと、色の見え方で読み違える。"""
    if percent is None:
        return ""
    width = max(0.0, min(100.0, percent))
    level = " high" if width >= 90 else (" mid" if width >= 70 else "")
    return (f'<span class="meter"><span class="meter-fill{level}"'
            f' style="width: {width:.0f}%"></span></span>')


def _amount(value: float) -> str:
    """数の書き方。 端数があるときだけ小数を出す —— 金額(OpenRouter)は $1.50 の
    ように出したいが、クレジットのような整数で 41,234.00 と出ると読みにくい。"""
    return f"{value:,.2f}" if value % 1 else f"{value:,.0f}"


def percent_text(value: float) -> str:
    """使用率の書き方。**端数は捨てない。**

    丸めていた頃は、**79.7% が「80% 使用」と出ていた** —— ワーカーは 80 未満なら
    その相手を使うので(`workers.QUOTA_LIMIT`)、画面を見た人には「上限を超えている
    のに使われた」に見える。判定は生の値で見ているので、**表示もそこへ合わせる**。

    **整数のときは小数を出さない**(`_amount` と同じ流儀)。端数の無い枠まで
    「100.0%」と書くと、読む数字が増えるだけになる。
    """
    return f"{value:.1f}" if value % 1 else f"{value:.0f}"


def _window_html(window: usage.Window) -> str:
    """枠 1 つぶん。使用率で言う相手と、金額で言う相手の両方を同じ形に収める。"""
    # **名前と戻る時刻には印を付ける** —— スマホではそこで行を割る(`pages.py` の
    # `quota-name` / `quota-reset`)。札の値の側は幅が狭く、1 行に流すと帯と数字の
    # 途中で折り返して読みにくい。広い画面では今までどおり 1 行に並べる
    parts = [f'<strong class="quota-name">{esc(window.label)}</strong>']
    if window.used_percent is not None:
        parts.append(
            # **数字と括弧はそれぞれ割らない**(`nowrap`)—— 狭い画面で「(残り」と
            # 「58.5%)」が別の行に泣き別れる。折り返すなら括弧の前で割る
            f"{_meter(window.used_percent)} "
            f'<span class="nowrap">{percent_text(window.used_percent)}% 使用</span>'
            f'<span class="nowrap">(残り {percent_text(window.remaining_percent)}%)</span>'
        )
    if window.used is not None:
        unit = f" {esc(window.unit)}" if window.unit else ""
        amount = f"{_amount(window.used)}{unit} 使用"
        if window.limit:
            amount += f" / 上限 {_amount(window.limit)}{unit}"
        parts.append(amount)
    if when := _when(window.resets_at):
        parts.append(f'<span class="muted quota-reset">{esc(when)} に戻る</span>')
    return " ".join(parts)


def _quota_cell(row: dict) -> str:
    quota: usage.Quota = row["quota"]
    if not quota.supported:
        # 「出せない」と書く。 空欄にすると「使っていない」と読めてしまう。
        return '<span class="muted">この相手は枠を出さない</span>'
    lines = [f"<div>{_window_html(w)}</div>" for w in quota.windows]
    # 数字が無いときに「◯時 時点」だけ出さない —— 何かが取れているように読める。
    if quota.windows and (fetched := _when(quota.fetched_at)):
        lines.append(f'<span class="muted">{esc(fetched)} 時点</span>')
    if quota.error:
        # 前の値は消さない。 一時的に繋がらないだけのことがあるので、
        # 直前まで見えていた数字と、そのあと失敗したことを並べて出す。
        lines.append(f'<span class="stale">⚠️ 取れませんでした: {esc(quota.error)}</span>')
    elif not quota.windows:
        lines.append('<span class="muted">まだ取っていない(「取り直す」を押す)</span>')
    # **1 つの塊に包む。** スマホでは欄が「見出し | 値」の 2 列の格子になり
    # (`pages.as_cards`)、中の部品が 1 つずつ升に入る —— 包まないと、
    # 「相手が言ったそのまま」などが見出しの側の列へ落ちる
    return "<div>" + "<br>".join(lines) + _raw_html(quota) + "</div>"


def _raw_html(quota: usage.Quota) -> str:
    """相手が言ったそのまま。**畳んで置く**(読みに来た人だけが開く)。

    **正規化した後の画面だけでは答えられないことがある。** 出ているのは Chiezo が
    付けた名前と割合で、窓の名前すら相手のものではない —— 相手が同じ名前の窓を
    2 つ返したときは、長さを添えて呼び分けている。「この行は何なのか」を確かめるには
    元を当たるしかないので、捨てずにここへ置く。

    **整形も翻訳もしない。** そのまま検索できることに値打ちがある
    (相手の資料や issue に当たるとき、こちらで直した文字列では引けない)。
    """
    if not quota.raw:
        return ""
    return (
        '<details class="raw-quota"><summary class="muted">相手が言ったそのまま</summary>'
        f"<pre>{esc(quota.raw)}</pre></details>"
    )


def _spent_cell(row: dict) -> str:
    spent = row["spent"]
    if not spent or not any(s.requests for s in spent.values()):
        return '<span class="muted">記録なし</span>'
    lines = []
    for name, _ in usage.SPENT_WINDOWS:
        value = spent.get(name)
        if value is None or not value.requests:
            continue
        text = f"{esc(_SPENT_LABELS.get(name, name))}: {value.requests} 回"
        # 0 と「言われていない」を分ける。 CLI ブリッジの相手はトークン数を返さないので、
        # 0 と書くと「0 トークンで動く相手」に見える。全部が未取得なら、そう言い切る。
        if value.input_tokens or value.output_tokens:
            # **トークン数には印を付ける** —— スマホでは「/」のあとで行を割り、
            # 一段下げて続ける(`pages.py` の `spent-detail`)。1 行に流すと数字の
            # 途中で折り返し、どの窓の数なのかが読みにくい
            detail = f"{value.input_tokens:,} in・{value.output_tokens:,} out"
            if value.unknown:
                detail += f' <span class="muted nowrap">(うち {value.unknown} 回は数なし)</span>'
            text += f' /<span class="spent-detail"> {detail}</span>'
        else:
            text += ' <span class="muted">(トークン数なし)</span>'
        lines.append(text)
    # 1 つの塊に包む(`_quota_cell` と同じ理由)。**窓ごとに <div> にする** ——
    # `<br>` でつなぐと、スマホで行を割った `spent-detail` のあとに空行ができる
    return "<div>" + "".join(f"<div>{line}</div>" for line in lines) + "</div>"


BREAKDOWN_ANCHOR = "ai-breakdown"

# 内訳に出す窓の既定。**5 時間では無人で回る層の一周が入らず、7 日では今日の跳ね上がりが
# 均される** —— 「いま枠を食っているのは誰か」を見に来る画面なので、その中間を既定にする。
BREAKDOWN_DEFAULT_WINDOW = "24h"

# 1 つの窓に出す行数。多い順に並べるので、枠を食っている組は必ず上に来る。
# 全部出すと、1 回しか呼ばれていない組が画面の大半を占める。
BREAKDOWN_ROWS = 20


def breakdown_window(request: Request | None = None) -> str:
    """内訳の窓をクエリから読む。

    **照合して通すのではなく、こちらが持っているほうを返す**(`_back_to` と同じ理由)。
    知らない値は既定へ倒す —— 窓が違うだけで、読めないものは何も無い。
    """
    raw = (request.query_params.get("spent_window") or "").strip() if request is not None else ""
    return next((name for name, _ in usage.SPENT_WINDOWS if name == raw), BREAKDOWN_DEFAULT_WINDOW)


def _window_links(current: str) -> str:
    """窓の切り替え。**いま見ている窓は押せなくする**(押しても同じ画面が出るだけ)。"""
    parts = []
    for name, _ in usage.SPENT_WINDOWS:
        label = esc(_SPENT_LABELS.get(name, name))
        if name == current:
            parts.append(f"<strong>{label}</strong>")
        else:
            parts.append(f'<a href="/admin/ai?spent_window={esc(name)}#{BREAKDOWN_ANCHOR}">{label}</a>')
    return " / ".join(parts)


def _breakdown_tokens(row: dict) -> str:
    """トークンの欄。**言わなかった(`unknown`)と 0 を分ける**(`_spent_cell` と同じ理由)。

    CLI を包んだ相手は 1 つも言わないので、全部が未取得なら言い切る ——
    そこを 0 と書くと「0 トークンで動く相手」に見える。
    """
    if row["unknown"] >= row["requests"]:
        return '<span class="muted">トークン数なし</span>'
    text = f"{row['input_tokens']:,} in・{row['output_tokens']:,} out"
    # **キャッシュから読んだぶんは入力の内訳**(足し込まない)。同じトークン数でも
    # 枠の減り方が違うので、分けて見えないと「重い呼び出し」を読み違える
    if row.get("cached_tokens"):
        text += f'<br><span class="muted">うち {row["cached_tokens"]:,} はキャッシュ</span>'
    if row["unknown"]:
        text += f' <span class="muted">(うち {row["unknown"]} 回は数なし)</span>'
    return text


def _breakdown_weight(row: dict) -> str:
    """やり取りの目方。**回数の隣に要る** —— トークン数を言わない相手では、
    20 KB の依頼と 300 KB の依頼が回数の上では同じ 1 回に見える。
    枠を食ったのがどちらかは、ここでしか分からない。
    """
    return (f'依頼 {esc(ai_history.size(row["prompt_bytes"]))}'
            f' → 応答 {esc(ai_history.size(row["reply_bytes"]))}')


def breakdown_html(request: Request | None = None) -> str:
    """使ったぶんの内訳 —— 相手 × モデル × 考える量 × 依頼元。

    **相手ごとの合計だけでは、詰まった枠の中身が読めない。** 同じ相手に、無人で回る層の
    巡回と外のアプリの依頼と手元からの問い合わせが混ざって入るので、
    「使い切った」と分かっても次にどれを止めればよいかが決まらない。
    """
    window = breakdown_window(request)
    span = next(delta for name, delta in usage.SPENT_WINDOWS if name == window)
    rows = usage_store.breakdown(datetime.now(UTC) - span)
    head = (f'<h4 id="{BREAKDOWN_ANCHOR}">内訳</h4>\n'
            f'<p class="muted">{_window_links(window)}</p>')
    if not rows:
        return f'{head}\n<p class="muted">この窓の記録はありません。</p>'
    shown = rows[:BREAKDOWN_ROWS]
    body = "\n".join(
        "<tr>"
        f'<td>{ai_history.who_html(r["provider"], r["model"], r["effort"])}</td>'
        f'<td>{ai_history.caller_html(r["caller"]) or "<span class=\"muted\">—</span>"}</td>'
        f'<td>{r["requests"]:,} 回</td>'
        f'<td class="snippet">{_breakdown_weight(r)}</td>'
        f"<td>{_breakdown_tokens(r)}</td>"
        "</tr>"
        for r in shown
    )
    rest = (f'<p class="muted">多い順に {BREAKDOWN_ROWS} 組まで'
            f"(この窓には {len(rows)} 組ありました)。</p>" if len(rows) > BREAKDOWN_ROWS else "")
    return f"""{head}
<table class="ai-settings ai-usage">
<thead><tr><th>AI</th><th>依頼元</th><th>回数</th><th>やり取りの目方</th><th>トークン</th></tr></thead>
<tbody>
{body}
</tbody>
</table>
{rest}"""


TRAIL_ANCHOR = "ai-quota-trail"

# 1 つの窓に出す観測点。畳んだ中に入るので多くてよいが、**古い点から読む価値が落ちる**
# (跳ねたのがいつかを当てたいので、見たいのは直近のほう)。
TRAIL_POINTS = 24


def _percent(value: float) -> str:
    """使用率の書き方。**小数を出さない** —— 相手が返すのは 0.1 刻みで、
    跳ねたかどうかの判断に小数第 1 位は効かない(桁が増えるぶん読みにくい)。
    """
    return f"{value:.0f}%"


def _climb(value: float) -> str:
    """上がったぶん。**「ポイント」と書く** —— 使用率どうしの差なので、
    「%」と書くと「前回の何 % 増えたのか」と読める。
    """
    return f"+{value:.0f} ポイント" if value >= 0.5 else "ほぼ動かず"


# 1 つの区間に並べる依頼の組。**上がった理由はたいてい上の 1〜2 組**なので、
# ここを増やしても読む手間が増えるだけ。
TRAIL_CALLS = 3


def _interval_calls(provider: str, start: str, end: str) -> str:
    """その区間に走っていた依頼。**使用率が動いた理由をここで読む。**

    使用率だけでは「跳ねた」までしか分からず、どれを止めればよいかが決まらない。
    **依頼元とモデルまで出す** —— 同じ相手でも、無人で回る層の巡回と外のアプリの
    依頼では止め方がまったく違う。
    """
    rows = usage_store.calls_between(provider, start, end)
    if not rows:
        return '<span class="muted">依頼なし</span>'
    shown = []
    for row in rows[:TRAIL_CALLS]:
        who = ai_inflight.caller_label(row["caller"]) or "(名乗りなし)"
        model = f' {row["model"]}' if row["model"] else ""
        shown.append(f'{esc(who)}{esc(model)} ×{row["requests"]}')
    rest = len(rows) - len(shown)
    if rest > 0:
        shown.append(f"ほか {rest} 組")
    total = sum(r["prompt_bytes"] for r in rows)
    weight = f' <span class="muted">依頼 {esc(ai_history.size(total))}</span>' if total else ""
    return " / ".join(shown) + weight


def _trail_points_html(trail: dict) -> str:
    """観測点を畳んで出す。**前回からの差と、そのあいだの依頼を添える** ——
    使用率だけを並べても、どこで跳ねたのかは引き算しながら読むことになるし、
    跳ねた理由は別の表を時刻で照らし合わせないと読めなかった。
    """
    points = trail["points"][-TRAIL_POINTS:]
    rows = []
    for i, point in enumerate(points):
        prior = points[i - 1] if i else None
        delta = ""
        calls = '<span class="muted">—</span>'
        if prior is not None:
            step = point["used_percent"] - prior["used_percent"]
            # **下がった差はそのまま出す**(窓が明けた印)。0 に丸めると、
            # 明けをまたいだ区間を「動かなかった」と読んでしまう
            delta = f"{step:+.0f}" if abs(step) >= 0.5 else "—"
            calls = _interval_calls(trail["provider"], prior["at"], point["at"])
        rows.append(
            f'<tr><td>{esc(_when(point["at"]))}</td>'
            f'<td>{esc(_percent(point["used_percent"]))}</td>'
            f'<td class="muted">{esc(delta)}</td>'
            f'<td class="snippet">{calls}</td></tr>'
        )
    return (
        f'<details><summary>{len(trail["points"])} 点</summary>'
        "<table><thead><tr><th>時刻</th><th>使用率</th><th>前回から</th>"
        "<th>そのあいだの依頼</th></tr></thead>"
        f'<tbody>{"".join(rows)}</tbody></table></details>'
    )


def _stale_note(trail: dict) -> str:
    """相手が返さなくなった窓の印。**消さずに、伸びていないことを書く。**

    窓の名前が変わると、その名前の線はそこで止まる —— 消すと「あの数字は何だったのか」
    を確かめようがなくなるが、生きている線と同じ顔で並んでいると読み違える。
    """
    if not trail.get("stale"):
        return ""
    return ('<br><span class="muted">いまは返ってこない窓'
            "(名前が変わったか、相手が出さなくなった)</span>")


def trail_html(request: Request | None = None) -> str:
    """枠の推移 —— いつ跳ねたかを読むところ。

    **控え(`quota`)は「いまどうか」しか持たない**ので、聞くたびに上書きされる。
    無人で回る層が枠を食っても、跳ねた時刻も 1 ポイントぶんの重さも後から読めなかった。

    **上がったぶんだけを足す**(`usage_store.quota_trail`)。窓は転がって明けるので、
    下がった差は「戻った」であって「使わなかった」ではない。
    """
    window = breakdown_window(request)
    span = next(delta for name, delta in usage.SPENT_WINDOWS if name == window)
    trails = usage_store.quota_trail(datetime.now(UTC) - span)
    head = f'<h4 id="{TRAIL_ANCHOR}">枠の推移</h4>'
    if not trails:
        return (f'{head}\n<p class="muted">この窓の観測はありません'
                "(枠を聞ける相手を呼ぶと、定時に控えはじめます)。</p>")
    body = "\n".join(
        f'<tr{" class=\"off\"" if t["stale"] else ""}>'
        f'<td>{esc(usage.label_of(t["provider"]))}<br>'
        f'<span class="muted">{esc(t["label"])}</span>{_stale_note(t)}</td>'
        f'<td>{esc(_percent(t["points"][-1]["used_percent"]))}</td>'
        f'<td>{esc(_climb(t["climbed"]))}</td>'
        f"<td>{_trail_points_html(t)}</td>"
        "</tr>"
        for t in trails
    )
    return f"""{head}
<table class="ai-settings ai-usage">
<thead><tr><th>AI / 枠</th><th>いま</th><th>この窓で上がったぶん</th><th>観測</th></tr></thead>
<tbody>
{body}
</tbody>
</table>
<p class="muted">上がったぶんだけを足している(窓が明けて下がった差は数えない)。
同じ窓の依頼は上の内訳で見る。</p>"""


def _refresh_button(row: dict, back: str, busy: bool = False) -> str:
    """行ごとの「取り直す」。

    **CLI が動いている相手は押せなくする**(`usage.busy_now`)。枠を聞くのは
    その相手の CLI をもう 1 本起こす動作で、走っている最中は空くまで待たされた
    うえで時間切れになる —— 押せるままだと、待たされてから失敗するだけになる。

    **押したら「取り直しています…」に変わる**(`data-busy`)。相手によっては
    往復に数十秒かかるので、何も変わらないと押せたのかどうかが分からない。
    """
    if not row["quota"].supported:
        return '<span class="muted">—</span>'
    if busy:
        return (
            '<button type="button" disabled title="この相手の CLI がいま動いています">'
            "取り直す</button>"
            '<span class="muted"> CLI 実行中</span>'
        )
    return (
        f'<form method="post" action="/admin/ai/usage" class="init-form">'
        f'<input type="hidden" name="provider" value="{esc(row["id"])}">'
        f'<input type="hidden" name="back" value="{esc(back)}">'
        f'<button type="submit" data-busy="取り直しています…">取り直す</button></form>'
    )


# 押した人を連れ出さないための行き先。**玄関にも同じボタンがある**ので、
# 書き切ると、どこから押しても AI の面へ飛ばされる(見ていた画面から追い出される)。
DEFAULT_BACK = "/admin/ai"

# 戻ってよい面。**このボタンを出している画面を名指しで並べる**。
#
# 「`/admin` で始まるもの」で通していた頃は、**外から来た文字列をそのまま行き先に
# 繋いでいた** —— 同じ生い立ちのままなので、読む側(と検査する側)には任意の URL を
# 作れるように見える。行き先が数えられる以上、数え上げるほうが確か。
BACK_PAGES = ("/admin", DEFAULT_BACK)


def _back_to(raw: str | None) -> str:
    """戻り先。**このボタンを出している面だけ**を通す(`BACK_PAGES`)。

    知らない行き先は既定へ倒す(断らないのは、戻れないだけで害が無いため)。

    **照合して通すのではなく、こちらが持っているほうを返す。** 見た目は同じでも、
    返した文字列が外から来たものかどうかが違う —— 外から来た値は、途中に検査が
    あっても「外から来た値」のまま行き先になる。返す先を数え上げてある以上、
    そこから返せば、行き先に外の文字列が入る道が 1 本も残らない。
    """
    path = (raw or "").strip()
    return next((page for page in BACK_PAGES if page == path), DEFAULT_BACK)


def _refresh_all_button() -> str:
    """まとめて取り直すボタン。 相手が 1 つも無いときは出さない
    —— 押しても何も起きないボタンは、壊れているのか設定が足りないのか読めない。

    **CLI が動いている相手は数に入れない**(押しても飛ばすので)。飛ばすことは
    ボタンの横に書く —— 黙って減らすと、押したあとの「N 件」が合わない。
    """
    targets = usage.refreshable()
    if not targets:
        return ('<p class="muted">まとめて取り直せる相手がいません'
                "(枠を聞ける相手を「使う」にすると出ます)。</p>")
    # **引くのは 1 回だけ**(相手ごとに引くと、相手の数だけ控えを読む)
    running = usage.busy_now()
    busy = [pid for pid in targets if pid in running]
    note = (
        f'<span class="muted">(CLI 実行中の {len(busy)} 件は飛ばします)</span>'
        if busy else ""
    )
    return refresh_all_form(
        f"使う相手の枠を全部取り直す({len(targets) - len(busy)} 件)"
    ) + note


def refresh_all_form(label: str, back: str = DEFAULT_BACK, klass: str = "init-form") -> str:
    """まとめて取り直すボタン 1 つぶん。**玄関からも使う**ので、ここが正。

    `back` に押した画面を渡すと、そこへ戻る(書き切ると連れ出される)。
    """
    return (
        f'<form method="post" action="/admin/ai/usage/all" class="{esc(klass)}">'
        f'<input type="hidden" name="back" value="{esc(back)}">'
        f'<button type="submit" data-busy="取り直しています…">{esc(label)}</button></form>'
    )


def banner_html(request: Request | None = None) -> str:
    """取り直した結果の 1 行。**表を出す画面はどれも出す**(玄関を含む)。

    押した画面へ戻ってくるので、戻った先に結果が出ないと、押したことが
    伝わらない(取れなかった相手がいても気づけない)。
    """
    q = request.query_params if request is not None else {}
    if refreshed := q.get("usage_refreshed"):
        label = esc(usage.label_of(refreshed))
        why = q.get("usage_error", "")
        return (
            f'<p class="stale">⚠️ {label} の使用量を取れません: {esc(why)}</p>' if why
            else f'<p class="note">✅ {label} の使用量を取り直しました。</p>'
        )
    if skipped := q.get("usage_skipped"):
        # **飛ばしたのは失敗ではない。** 相手が落ちているわけではなく、
        # CLI が空けば押せる —— 次にどうすればよいかまで書く
        done = q.get("usage_refreshed_all")
        got = f"{esc(done)} 件を取り直しました。" if done is not None else ""
        return (
            f'<p class="note">{got}{esc(skipped)} は CLI が動いていたので'
            "飛ばしました(終わってからもう一度押してください)。</p>"
        )
    if (done := q.get("usage_refreshed_all")) is not None:
        # 一気に取り直したとき。 取れた数と、取れなかった相手を並べる ——
        # 「全部取り直しました」とだけ書くと、落ちている相手に気づけない。
        why = q.get("usage_error", "")
        return (
            f'<p class="stale">⚠️ {esc(done)} 件を取り直しました'
            f"(取れなかった相手: {esc(why)})。</p>" if why
            else f'<p class="note">✅ {esc(done)} 件の使用量を取り直しました。</p>'
        )
    return ""


def table_html(rows: list[dict], back: str = DEFAULT_BACK) -> str:
    """使用量の表 1 つぶん。**玄関と AI の面で同じものを出す**ので、ここが正。

    窓は相手が返したぶんを全部並べる —— いちばん詰まっている 1 つだけに
    畳むと、5 時間の窓しか見えない相手が出る。重い仕事を頼んでよいかは、
    **その仕事が収まる窓**(長い依頼なら週のほう)で決まるため。

    `back` は行ごとの「取り直す」を押した人の戻り先。

    **走っている相手は 1 回だけ引く**(`usage.busy_now`)—— 行ごとに引くと、
    表を 1 枚描くあいだに控えを相手の数だけ読むことになる。
    """
    busy = usage.busy_now()
    body = "\n".join(
        f'<tr{"" if row["enabled"] else ' class="off"'}>'
        f'<td>{esc(row["label"])}</td>'
        f"<td>{_quota_cell(row)}</td>"
        f"<td>{_spent_cell(row)}</td>"
        f'<td>{_refresh_button(row, back, row["id"] in busy)}</td></tr>'
        for row in rows
    )
    return f"""<table class="ai-settings ai-usage">
<thead><tr><th>AI</th><th>相手が言う枠(残り)</th><th>Chiezo が使ったぶん</th><th></th></tr></thead>
<tbody>
{body}
</tbody>
</table>"""


def section_html(request: Request | None = None) -> str:
    """管理画面に差し込む「使用量」節。"""
    if not usage_store.is_enabled():
        return (
            f'<h3 id="{SECTION_ANCHOR}">使用量</h3>\n'
            '<p class="muted">記録の置き場がありません。書き込み可能なディレクトリを'
            " <code>CHIEZO_STATE_DIR</code> に設定すると、使用量を出せるようになります。</p>"
        )

    since = _when(usage_store.first_recorded_at() or "")
    since_note = (
        f'<p class="muted">「Chiezo が使ったぶん」は {esc(since)} からの記録です。</p>'
        if since else
        '<p class="muted">「Chiezo が使ったぶん」の記録はまだありません'
        "(相手を呼ぶと溜まりはじめます)。</p>"
    )

    return f"""<h3 id="{SECTION_ANCHOR}">使用量</h3>
{banner_html(request)}
<details>
<summary>この節について</summary>
<p><strong>数が 2 つあるのは、測っているものが違うから。</strong>
「相手が言う枠」は相手の勘定なので<strong>残りが分かる</strong>が、
<strong>聞ける相手が限られる</strong>。「Chiezo が使ったぶん」は Chiezo の勘定なので
<strong>全部の相手で同じ物差し</strong>だが、<strong>残りは分からない</strong>
—— <strong>Chiezo を通していない利用は入らない</strong>(手元の端末で回した CLI など)。</p>
<p><strong>枠を聞けるのは CLI を包んだ 3 つと ElevenLabs・OpenRouter だけ。</strong>
Gemini は残量が Google Cloud の Quotas API 側にあり、OpenAI は Admin キーが要るので、
どちらもここに入れる鍵では引けない。
聞ける相手は<strong>モデルを呼ばずに聞く</strong>(確かめるたびに枠を食っては本末転倒なので)。</p>
<p><strong>開いたときには聞きに行かない。</strong>控えてある値と「いつ取ったか」を出し、
取り直しはボタンで —— 落ちている相手がいると、その数だけ画面が遅れるため。
まとめて取り直すボタンは<strong>並行に聞く</strong>ので、待つのは一番遅い相手のぶんだけ。
<strong>枠の欄の「相手が言ったそのまま」</strong>には、正規化する前の返事が入っている ——
画面に出ている名前は Chiezo が付けたもの(相手が同じ名前の窓を 2 つ返したときは、
窓の長さを添えて呼び分ける)なので、<strong>その行が何なのかは元を当たらないと読めない</strong>。
API からは <code>GET /v1/ai/usage</code>(取り直すなら <code>?refresh=1</code>)。</p>
</details>
{since_note}
{_refresh_all_button()}
{table_html(usage.rows())}
{breakdown_html(request)}
{trail_html(request)}
"""


@router.post("/admin/ai/usage/all")
async def refresh_all_usage(back: str = Form(DEFAULT_BACK)):
    """枠を聞ける相手をまとめて取り直す(並行に聞く)。

    行ごとに押すと相手の数だけ往復することになる。取れなかった相手がいても
    残りは取り直し、誰が取れなかったかを画面に出す。

    **押した画面へ戻す**(`back`)。玄関にも同じボタンがあるので、行き先を
    書き切ると、どこから押しても AI の面へ連れて行かれる。
    """
    done, skipped = await usage.refresh_all()
    failed = [usage.label_of(pid) for pid, quota in done.items() if quota.error]
    params: dict[str, object] = {"usage_refreshed_all": len(done) - len(failed)}
    if failed:
        params["usage_error"] = "、".join(failed)[:300]
    if skipped:
        # **飛ばしたことは書く。** 黙って減らすと、数が合わないのか
        # 取れなかったのかが読めない
        params["usage_skipped"] = "、".join(usage.label_of(pid) for pid in skipped)[:300]
    return RedirectResponse(
        f"{_back_to(back)}?{urlencode(params)}#{SECTION_ANCHOR}", status_code=303
    )


@router.post("/admin/ai/usage")
async def refresh_usage(provider: str = Form(...), back: str = Form(DEFAULT_BACK)):
    """1 相手ぶん取り直す(結果はクエリで画面へ返す)。

    **押した画面へ戻す**(`back`)。玄関にも同じ表があるので、行き先を書き切ると
    どこから押しても AI の面へ連れて行かれる(まとめて取り直すボタンと同じ理由)。
    """
    spec = usage.spec_of(provider)
    if spec is None:
        raise HTTPException(404, {"error": f"unknown provider: {provider}"})
    if not spec.usage:
        raise HTTPException(400, {"error": f"「{spec.label}」は使用量を出しません"})
    if spec.id in usage.busy_now():
        # ボタンは disabled にしてあるが、**描いたあとに走り出すことがある**
        # (無人で回る層が動かす)。断るほうが、待たされて時間切れになるより早い
        return RedirectResponse(
            f"{_back_to(back)}?{urlencode({'usage_skipped': spec.label})}#{SECTION_ANCHOR}",
            status_code=303,
        )
    quota = await usage.refresh(spec.id)
    params = {"usage_refreshed": spec.id}
    if quota.error:
        params["usage_error"] = quota.error[:300]
    return RedirectResponse(
        f"{_back_to(back)}?{urlencode(params)}#{SECTION_ANCHOR}", status_code=303
    )
