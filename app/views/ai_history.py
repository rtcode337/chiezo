"""管理画面の「AI への依頼」節 —— 成功も失敗も、新しい順に 1 枚の表で見せる。

**分けない。** 失敗だけを見ていた頃は「落ちていないのに結果が変」「そもそも呼べていた
のか」が読めなかった。何を頼んで、どれが通って、どれが落ちたかは同じ 1 本の流れなので、
同じ表に並べて**結果の欄で見分ける**。失敗だけを見たいときは絞り込みで足りる。

**素材は 2 か所にある。** 成功は `app/usage_store.py`(枠の集計と同じ表)、失敗は
`app/ai_log.py`。片方に寄せると壊れるものがある —— 失敗を集計側に混ぜると
「使ったぶん」が水増しになり、成功を失敗の控えに混ぜると 500 件の枠がすぐ埋まる。
**並べるときだけ突き合わせる**のが、どちらの意味も壊さない置き方。

**ページングを入れてある。** 無人で回る層(収集の時計)ができたので、依頼は放っておいても
増える。全部出すと画面が縦に伸び続けて、いちばん見たい直近が埋もれる。
"""
from __future__ import annotations

from app import ai_log, settings_store, usage_store
from app.jst import format as format_jst
from app.jst import parse as parse_jst
from app.pages import esc

SECTION_ANCHOR = "ai-history"

# 1 ページの件数。**直近が読めれば足りる**ので少なく取る(深く辿るのは稀)
PAGE_SIZE = 10

# 突き合わせのために読む上限。ページを深く辿るほど要るが、どちらの表も
# 有限(失敗 500 件 / 成功 30 日)なので、この程度で頭打ちにしてよい
MAX_SCAN = 500


def _when(raw: str) -> str:
    parsed = parse_jst(raw)
    return format_jst(parsed) if parsed else raw


def _status(status: int) -> str:
    # 0 は「そもそも繋がらなかった」。数字の 0 だけだと成功に読める
    return "届かず" if not status else str(status)


def _size(nbytes: int) -> str:
    if nbytes >= 1024 * 1024:
        return f"{nbytes / 1024 / 1024:.1f} MB"
    if nbytes >= 1024:
        return f"{nbytes / 1024:.0f} KB"
    return f"{nbytes} B"


def _tokens(row: dict) -> str:
    """使ったトークン。**None は「相手が言わなかった」、0 は「使わなかった」**。

    混ぜると、数を返さない相手(CLI ブリッジ)が「0 トークンで動く相手」に見える。
    """
    parts = [
        f"入 {row['input_tokens']:,}" if row["input_tokens"] is not None else "",
        f"出 {row['output_tokens']:,}" if row["output_tokens"] is not None else "",
    ]
    body = " / ".join(p for p in parts if p)
    return body or '<span class="muted">相手が言わなかった</span>'


def entries(failed_only: bool = False) -> list[dict]:
    """成功と失敗を新しい順に混ぜた一覧。"""
    rows = [{**r, "ok": False} for r in ai_log.recent(MAX_SCAN)]
    if not failed_only:
        rows += [{**r, "ok": True} for r in usage_store.recent_calls(MAX_SCAN)]
    # 同じ秒に並んだものの順は決められないので、時刻だけで並べる(見た目の話)
    return sorted(rows, key=lambda r: r.get("at") or "", reverse=True)


def _pager(page: int, total: int, failed_only: bool) -> str:
    """前後のページへのリンク。**JS を持たないのでクエリで送る**(画面全体と同じ流儀)。"""
    last = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    def link(target: int, label: str) -> str:
        if target < 1 or target > last or target == page:
            return f'<span class="muted">{label}</span>'
        q = f"?ai_page={target}" + ("&ai_failed=1" if failed_only else "")
        return f'<a href="{q}#{SECTION_ANCHOR}">{label}</a>'
    return (
        f'<div class="pager">{link(page - 1, "← 新しい")}'
        f'<span class="muted">{page} / {last} ページ({total:,} 件)</span>'
        f'{link(page + 1, "古い →")}</div>'
    )


def section_html(page: int = 1, failed_only: bool = False) -> str:
    if not settings_store.state_dir():
        return (
            f'<h3 id="{SECTION_ANCHOR}">AI への依頼</h3>\n'
            '<p class="muted">記録の置き場がありません。書き込み可能なディレクトリを'
            " <code>CHIEZO_STATE_DIR</code> に設定すると、依頼の控えが残るようになります。</p>"
        )

    rows = entries(failed_only)
    # **範囲の外は最後のページに寄せる**。空の表と「2 / 1 ページ」を見せても
    # 何が起きたのか読めない(絞り込みを切り替えると件数が減るので普通に起こる)
    last = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(1, page), last)
    shown = rows[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]

    toggle = (
        f'<a href="?#{SECTION_ANCHOR}">すべて見る</a>' if failed_only
        else f'<a href="?ai_failed=1#{SECTION_ANCHOR}">失敗だけ見る</a>'
    )
    if not rows:
        empty = "まだ落ちていません。" if failed_only else "まだ何も頼んでいません。"
        return (
            f'<h3 id="{SECTION_ANCHOR}">AI への依頼</h3>\n'
            f'<p class="muted">{empty} {toggle}</p>'
        )

    body = []
    for row in shown:
        kind = esc(ai_log.kind_label(row.get("kind") or ai_log.KIND_CHAT))
        who = esc(row["backend"])
        if row.get("model"):
            who += f'<br><span class="muted">{esc(row["model"])}</span>'
        if row["ok"]:
            result = '<span class="muted">成功</span>'
            detail = _tokens(row)
        else:
            result = f'<span class="stale">{esc(_status(row["status"]))}</span>'
            detail = (
                f'<span class="snippet">{esc(row["reason"])}</span>'
                f'<br><span class="muted">依頼文 {esc(_size(row["prompt_bytes"]))}</span>'
            )
        body.append(
            f'<tr{"" if row["ok"] else ' class="off"'}>'
            f"<td>{esc(_when(row['at']))}</td><td>{kind}</td><td>{who}</td>"
            f"<td>{result}</td><td>{detail}</td></tr>"
        )

    return f"""<h3 id="{SECTION_ANCHOR}">AI への依頼</h3>
<p class="muted">
会話・絵・音・動画・声のどれでも、頼んだものは新しい順にここへ残る。
<strong>プロンプトと応答は残していない</strong> —— 呼んだ側の材料がそのまま入るため。
失敗のときだけ理由と依頼文の大きさを残してあるのは、失敗が大きさに寄っているのかを
後から見分けられるようにするため。{toggle}<br>
成功の控えは {usage_store.KEEP_DAYS} 日、失敗の控えは直近 {ai_log.MAX_ROWS} 件まで。
機械で読むなら <code>GET /v1/ai/failures</code>。
</p>
<table>
<thead>
<tr><th>日時(JST)</th><th>依頼</th><th>相手</th><th>結果</th><th>中身</th></tr>
</thead>
<tbody>
{"".join(body)}
</tbody>
</table>
{_pager(page, len(rows), failed_only)}"""
