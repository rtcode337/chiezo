"""管理画面の「使用量」節 —— 各 AI の枠(残り)と、Chiezo が使ったぶん。

「AI の相手」の表とは分けてある。 あちらは設定を一度入れたら開かない場所、
こちらは何度も見に来る場所で、読む目的が違う(そして相手を選ぶための情報でもある ——
枠を使い切っている相手には重い仕事を頼まない)。

描画のときに相手へ問い合わせない(「接続を試す」と同じ流儀)。控えてある値と
「いつ取ったか」を出し、取り直しは行ごとのボタンで。落ちている相手がいても画面は遅れない。
"""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import jst, providers, usage, usage_store
from app.pages import esc

router = APIRouter()

SECTION_ANCHOR = "ai-usage"
BACK_TO_SECTION = f"/admin#{SECTION_ANCHOR}"

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


def _window_html(window: usage.Window) -> str:
    """枠 1 つぶん。使用率で言う相手と、金額で言う相手の両方を同じ形に収める。"""
    parts = [f"<strong>{esc(window.label)}</strong>"]
    if window.used_percent is not None:
        parts.append(
            f"{_meter(window.used_percent)} {window.used_percent:.0f}% 使用"
            f"(残り {window.remaining_percent:.0f}%)"
        )
    if window.used is not None:
        unit = f" {esc(window.unit)}" if window.unit else ""
        amount = f"{_amount(window.used)}{unit} 使用"
        if window.limit:
            amount += f" / 上限 {_amount(window.limit)}{unit}"
        parts.append(amount)
    if when := _when(window.resets_at):
        parts.append(f'<span class="muted">{esc(when)} に戻る</span>')
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
    return "<br>".join(lines)


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
            text += f" / {value.input_tokens:,} in・{value.output_tokens:,} out"
            if value.unknown:
                text += f' <span class="muted">(うち {value.unknown} 回は数なし)</span>'
        else:
            text += ' <span class="muted">(トークン数なし)</span>'
        lines.append(text)
    return "<br>".join(lines)


def _refresh_button(row: dict) -> str:
    if not row["quota"].supported:
        return '<span class="muted">—</span>'
    return (
        f'<form method="post" action="/admin/ai/usage" class="init-form">'
        f'<input type="hidden" name="provider" value="{esc(row["id"])}">'
        f'<button type="submit">取り直す</button></form>'
    )


def section_html(request: Request | None = None) -> str:
    """管理画面に差し込む「使用量」節。"""
    if not usage_store.is_enabled():
        return (
            f'<h2 id="{SECTION_ANCHOR}">使用量</h2>\n'
            '<p class="muted">記録の置き場がありません。書き込み可能なディレクトリを'
            " <code>CHIEZO_STATE_DIR</code> に設定すると、使用量を出せるようになります。</p>"
        )

    banner = ""
    q = request.query_params if request is not None else {}
    if refreshed := q.get("usage_refreshed"):
        label = esc(providers.label_of(refreshed))
        why = q.get("usage_error", "")
        banner = (
            f'<p class="stale">⚠️ {label} の使用量を取れません: {esc(why)}</p>' if why
            else f'<p class="note">✅ {label} の使用量を取り直しました。</p>'
        )

    rows = []
    for row in usage.rows():
        rows.append(
            f'<tr{"" if row["enabled"] else ' class="off"'}>'
            f'<td>{esc(row["label"])}</td>'
            f"<td>{_quota_cell(row)}</td>"
            f"<td>{_spent_cell(row)}</td>"
            f"<td>{_refresh_button(row)}</td></tr>"
        )

    since = _when(usage_store.first_recorded_at() or "")
    since_note = (
        f'<p class="muted">「Chiezo が使ったぶん」は {esc(since)} からの記録です。</p>'
        if since else
        '<p class="muted">「Chiezo が使ったぶん」の記録はまだありません'
        "(相手を呼ぶと溜まりはじめます)。</p>"
    )

    return f"""<h2 id="{SECTION_ANCHOR}">使用量</h2>
{banner}
<details>
<summary>この節について</summary>
<p><strong>数が 2 つあるのは、測っているものが違うから。</strong>
「相手が言う枠」は相手の勘定なので<strong>残りが分かる</strong>が、
<strong>聞ける相手が限られる</strong>。「Chiezo が使ったぶん」は Chiezo の勘定なので
<strong>全部の相手で同じ物差し</strong>だが、<strong>残りは分からない</strong>
—— <strong>Chiezo を通していない利用は入らない</strong>(手元の端末で回した CLI など)。</p>
<p><strong>枠を聞けるのは Claude Code CLI・Codex CLI・Antigravity CLI・OpenRouter だけ。</strong>
Gemini は残量が Google Cloud の Quotas API 側にあり、OpenAI は Admin キーが要るので、
どちらもここに入れる鍵では引けない。CLI の 3 つは<strong>モデルを呼ばずに聞く</strong>
(確かめるたびに枠を食っては本末転倒なので)。</p>
<p><strong>開いたときには聞きに行かない。</strong>控えてある値と「いつ取ったか」を出し、
取り直しは行のボタンで —— 落ちている相手がいると、その数だけ画面が遅れるため。
API からは <code>GET /v1/ai/usage</code>(取り直すなら <code>?refresh=1</code>)。</p>
</details>
{since_note}
<table class="ai-settings ai-usage">
<thead><tr><th>AI</th><th>相手が言う枠(残り)</th><th>Chiezo が使ったぶん</th><th></th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody>
</table>
"""


@router.post("/admin/ai/usage")
async def refresh_usage(provider: str = Form(...)):
    """1 相手ぶん取り直す(結果はクエリで画面へ返す)。"""
    spec = usage.spec_of(provider)
    if spec is None:
        raise HTTPException(404, {"error": f"unknown provider: {provider}"})
    if not spec.usage:
        raise HTTPException(400, {"error": f"「{spec.label}」は使用量を出しません"})
    quota = await usage.refresh(spec.id)
    params = {"usage_refreshed": spec.id}
    if quota.error:
        params["usage_error"] = quota.error[:300]
    return RedirectResponse(f"/admin?{urlencode(params)}#{SECTION_ANCHOR}", status_code=303)
