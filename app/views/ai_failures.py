"""管理画面の「AI 依頼の失敗」節 —— 会話も生成も、落ちたものを 1 枚に並べる。

なぜ画面に要るか。 控え自体は前からあった(`app/ai_log.py`)が、読む手段が
`GET /v1/ai/failures` の JSON しかなかった。**失敗に気づく人は、たいてい
その場に居合わせていない** —— 朝の定期実行が落ちた、絵を頼んだが出てこない、と
後から確かめに来るので、管理画面を開けば読めるようにしておく。

会話と生成を分けない。 分けると「何を頼んだかによって見る場所が違う」ことになり、
どちらで落ちたか分かっていない人が探せない。種類は列で示す。

中身(プロンプト・応答)は出さない。 控えがそもそも持っていない
(`app/ai_log.py` の決めごと)。出せるのは相手・種類・状態・理由と大きさまで。
"""
from __future__ import annotations

from app import ai_log, jst, settings_store
from app.pages import esc

SECTION_ANCHOR = "ai-failures"

# 一度に出す件数。 読むのは「最近なにが落ちたか」で、全部を眺める場所ではない。
SHOW_ROWS = 30


def _when(raw: str) -> str:
    """記録の時刻を JST の 1 行に。読めない値はそのまま出す(画面を落とさない)。"""
    when = jst.parse(raw)
    return jst.format(when) if when else raw


def _status(status: int) -> str:
    """相手が返した状態。**0 は「そもそも繋がらなかった」**ので、そう書く ——
    数字の 0 だけだと「成功」と読めてしまう。"""
    return "届かず" if not status else str(status)


def _size(nbytes: int) -> str:
    if nbytes >= 1024 * 1024:
        return f"{nbytes / 1024 / 1024:.1f} MB"
    if nbytes >= 1024:
        return f"{nbytes / 1024:.0f} KB"
    return f"{nbytes} B"


def section_html() -> str:
    """管理画面に差し込む「AI 依頼の失敗」節。"""
    if not settings_store.state_dir():
        return (
            f'<h2 id="{SECTION_ANCHOR}">AI 依頼の失敗</h2>\n'
            '<p class="muted">記録の置き場がありません。書き込み可能なディレクトリを'
            " <code>CHIEZO_STATE_DIR</code> に設定すると、失敗の控えが残るようになります。</p>"
        )

    rows = ai_log.recent(SHOW_ROWS)
    if not rows:
        return (
            f'<h2 id="{SECTION_ANCHOR}">AI 依頼の失敗</h2>\n'
            '<p class="muted">まだ記録がありません（落ちていないか、まだ何も頼んでいない）。</p>'
        )

    body = "\n".join(
        "<tr>"
        f"<td>{esc(_when(row['at']))}</td>"
        f"<td>{esc(ai_log.kind_label(row.get('kind') or ai_log.KIND_CHAT))}</td>"
        f"<td>{esc(row['backend'])}"
        + (f"<br><span class=\"muted\">{esc(row['model'])}</span>" if row["model"] else "")
        + "</td>"
        f"<td>{esc(_status(row['status']))}</td>"
        f"<td class=\"snippet\">{esc(row['reason'])}</td>"
        f"<td>{esc(_size(row['prompt_bytes']))}</td>"
        "</tr>"
        for row in rows
    )

    return f"""<h2 id="{SECTION_ANCHOR}">AI 依頼の失敗</h2>
<p class="muted">
会話・絵・音・動画・声のどれでも、落ちたものは新しい順にここへ残る（直近 {ai_log.MAX_ROWS} 件まで）。
<strong>プロンプトと応答は残していない</strong> —— 呼んだ側の材料がそのまま入るため。
大きさだけ残してあるのは、失敗が大きさに寄っているのかを後から見分けられるようにするため。
機械で読むなら <code>GET /v1/ai/failures</code>。
</p>
<table>
<thead>
<tr><th>日時（JST）</th><th>依頼</th><th>相手</th><th>状態</th><th>理由</th><th>依頼文の大きさ</th></tr>
</thead>
<tbody>
{body}
</tbody>
</table>"""
