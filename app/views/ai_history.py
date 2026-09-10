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

from datetime import UTC, datetime

from app import ai_inflight, ai_log, media, settings_store, usage_store
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
    **言わなかったときは何も返さない** —— そのぶんは目方(`_weight`)が埋める。
    """
    parts = [
        f"入 {row['input_tokens']:,}" if row["input_tokens"] is not None else "",
        f"出 {row['output_tokens']:,}" if row["output_tokens"] is not None else "",
    ]
    return " / ".join(p for p in parts if p)


def _took(ms: int) -> str:
    if ms >= 60_000:
        return f"{ms // 60_000} 分 {ms % 60_000 // 1000} 秒"
    if ms >= 1000:
        return f"{ms / 1000:.1f} 秒"
    return f"{ms} ミリ秒"


def _weight(row: dict) -> str:
    """やり取りの目方。**「どういうやり取りだったか」はここで読む。**

    中身は残していないので、読めるのは大きさと時間だけ —— それでも
    「短く聞いて長く答えさせた」「絵を 1 枚描かせて 6 分待った」の区別は付く。
    トークン数を言わない相手(CLI ブリッジ・絵と音)ではこちらが唯一の手がかりになる。
    """
    sent, got, ms = row.get("prompt_bytes"), row.get("reply_bytes"), row.get("ms")
    flow = " → ".join(
        p for p in (
            f"依頼 {_size(sent)}" if sent is not None else "",
            f"応答 {_size(got)}" if got is not None else "",
        ) if p
    )
    return " / ".join(p for p in (flow, _took(ms) if ms is not None else "") if p)


def _detail(row: dict) -> str:
    """成功した呼び出しの「中身」の欄。

    **目方を先に出す。** トークン数は相手が言ったときだけ足す —— 以前は
    トークンだけを出していたので、言わない相手の行が「相手が言わなかった」の
    一言になり、成功したのに何をした呼び出しなのか読めなかった。
    """
    lines = [p for p in (_weight(row), _tokens(row)) if p]
    if not lines:
        # 目方を残す前の古い控え。**「言わなかった」ではない**(こちらが測っていない)
        return '<span class="muted">控えは回数だけ</span>'
    body = f'<span class="snippet">{lines[0]}</span>'
    if len(lines) > 1:
        body += f'<br><span class="muted">{lines[1]}</span>'
    return body


def _elapsed(started: str) -> str:
    """始まってからの経過。**走っている行はこれが要**で、日時だけでは
    「遅い」のか「止まっている」のかが読めない。"""
    at = parse_jst(started)
    if at is None:
        return ""
    secs = int((datetime.now(UTC) - at).total_seconds())
    return _took(max(secs, 0) * 1000)


def running_rows() -> list[dict]:
    """いま走っているものを、表に並べられる形で新しい順に。

    **会話と生成を 1 本に混ぜる**(`entries` が成功と失敗を混ぜるのと同じ理由)。
    見る人は「AI に頼んだことが走っているか」を知りたいのであって、それが会話だったか
    絵だったかを先に知ってはいない。
    """
    rows = [
        {
            "at": r["at"],
            "kind": r.get("kind") or ai_log.KIND_CHAT,
            "backend": r["backend"],
            "model": r.get("model") or "",
            "state": "走っている",
            "prompt_bytes": r.get("prompt_bytes"),
        }
        for r in ai_inflight.running()
    ]
    rows += [
        {
            "at": j["created_at"],
            "kind": j.get("kind") or "",
            "backend": j.get("backend") or "",
            "model": j.get("model") or "",
            # 生成は順番待ちがある。**待ちと走行を混ぜない** —— 混ぜると
            # 「相手が遅い」と「自分の順番がまだ」の区別が付かない
            "state": "順番待ち" if j.get("state") == "queued" else "走っている",
            # 依頼文そのものは出さない(この表は中身を持たない約束)。大きさだけ
            "prompt_bytes": len((j.get("prompt") or "").encode()),
            # **止められるのは生成だけ。** 会話(`ai_inflight`)は相手との 1 往復で、
            # 掴んでいるのは呼んだ側のタスクなので、この画面からは手が届かない
            "job_id": j.get("id") or "",
        }
        for j in media.running_jobs()
    ]
    return sorted(rows, key=lambda r: r.get("at") or "", reverse=True)


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

    # 走っているものは**ページから外して常に先頭に出す**。控えの表は新しい順に
    # 送っていくものだが、走っているぶんは「いまの状態」なので、2 ページ目を見て
    # いるあいだに見えなくなっては用をなさない。件数にも数えない(結果がまだ無い)。
    running = running_rows()
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
    if not rows and not running:
        empty = "まだ落ちていません。" if failed_only else "まだ何も頼んでいません。"
        return (
            f'<h3 id="{SECTION_ANCHOR}">AI への依頼</h3>\n'
            f'<p class="muted">{empty} {toggle}</p>'
        )

    body = []
    for row in running:
        who = esc(row["backend"])
        if row["model"]:
            who += f'<br><span class="muted">{esc(row["model"])}</span>'
        detail = f'<span class="muted">依頼 {esc(_size(row["prompt_bytes"]))}</span>'
        # **暴走したものを止める口。** 押すと待ち枠がすぐ空くので、後ろで並んでいる
        # ぶんが先へ進める(向こう側の CLI は自分の時間切れまで走り続ける)
        if row.get("job_id"):
            detail += (
                f'<form method="post" action="/admin/media/{esc(row["job_id"])}/cancel"'
                ' onsubmit="return confirm(\'この生成を止めますか。'
                '向こう側の処理はすぐには止まりません\')">'
                '<button type="submit" class="danger">止める</button></form>'
            )
        body.append(
            '<tr class="job-status running">'
            f"<td>{esc(_when(row['at']))}</td>"
            f"<td>{esc(ai_log.kind_label(row['kind']))}</td><td>{who}</td>"
            f'<td>{esc(row["state"])}<br>'
            f'<span class="muted">{esc(_elapsed(row["at"]))}</span></td>'
            f"<td>{detail}</td></tr>"
        )
    for row in shown:
        kind = esc(ai_log.kind_label(row.get("kind") or ai_log.KIND_CHAT))
        who = esc(row["backend"])
        if row.get("model"):
            who += f'<br><span class="muted">{esc(row["model"])}</span>'
        if row["ok"]:
            result = '<span class="muted">成功</span>'
            detail = _detail(row)
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

    # 走っているものがあるときだけ読み直す導線を出す。画面は JS を持たないので、
    # 自分では変わらない —— 出しっぱなしにすると、何も走っていないのに
    # 「更新すれば何か出る」と読める
    now_running = (
        f'<br><strong>いま {len(running)} 件走っている。</strong>'
        f'<a href="?#{SECTION_ANCHOR}">進み具合を読み直す</a>'
        if running else ""
    )
    return f"""<h3 id="{SECTION_ANCHOR}">AI への依頼</h3>
<p class="muted">
会話・絵・音・動画・声のどれでも、頼んだものは新しい順にここへ残る。
<strong>走っている最中のものは表の先頭</strong>に出て、終わると結果の行に変わる ——
控えが書かれるのは往復が終わってからなので、これが無いと、無人で回っているぶんは
遅いのか止まっているのか呼べてすらいないのかが読めない。{now_running}
<strong>プロンプトと応答は残していない</strong> —— 呼んだ側の材料がそのまま入るため。
かわりに<strong>やり取りの目方</strong>(依頼文と応答の大きさ・かかった時間)を残してあるので、
中身を持たずに「短く聞いて長く答えさせた」「絵を 1 枚描かせて何分も待った」の区別は付く。
失敗のときは理由と依頼文の大きさを残す —— 失敗が大きさに寄っているのかを
後から見分けられるようにするため。{toggle}<br>
成功の控えは {usage_store.KEEP_DAYS} 日、失敗の控えは直近 {ai_log.MAX_ROWS} 件まで。
機械で読むなら <code>GET /v1/ai/failures</code>、走っているものは
<code>GET /v1/ai/inflight</code>。
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
