"""管理画面の「見比べ」の面(`/admin/media`)。

何案か作らせたものを、依頼文つきで並べて選ぶところ。**音は AI 自身が聴けない**ので、
聴き比べる手段がここにしかない。

なぜ管理画面にも置くか。 見比べはもともとやること層(`tasks-frontend/`)の画面
だったが、あれはタスクの入れ物で、**見比べているのは生成物** —— 相手・鍵・使用量・
依頼の履歴と同じ「AI に頼んだこと」の面にある。設定を見に来た人が、そのまま
出来たものを見て選べる。

決めごと:

- **生成の口は置かない**(やること層と同じ線)。あるのは読むのと印を付けるところまで。
  課金の走る操作は、画面から押せる場所に置かない
- **JS を持たない**(管理画面の流儀)。絵は `<img>`、音と動画はブラウザ内蔵の
  プレイヤー。並べ替えも絞り込みもフォームで送る
- **大きく見るのは CSS の `:target`**(`_modal`)。並べるための小さい絵と、
  読むための全画面は要求が逆 —— 並べるほうを優先すると 1 案ぶんが読めなくなる。
  リンクで `#` を付ければ開き、別の `#` へ移れば閉じるので、JS を持たずに済む。
  **閉じる先はその案のカード**にする —— `#` だけに戻すとページの先頭へ飛んで、
  どれを見ていたのか分からなくなる
- **一覧と中身で口を分ける**(`media.job_groups` / `job_group` と同じ理由)。
  一覧に全部の案を積むと、開くつもりのない依頼の絵と音まで毎回運ぶ(音は 1 本で数 MB)
- **印を付けたら組の画面へ戻す**(303)。押した場所が見えたままでないと、
  どれに付けたのかを確かめるのにもう一度探すことになる
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import media, media_providers
from app.jst import format as format_jst
from app.jst import parse as parse_jst
from app.pages import esc, page_shell

log = logging.getLogger("chiezo.app")

router = APIRouter()

# 一覧に出す組の数。**溜まるものなので頭打ちにする** —— 生成は放っておいても増え、
# 全部出すといちばん見たい直近が埋もれる(依頼の履歴と同じ扱い)。
GROUPS_PER_PAGE = 20

# 畳んだ文章の頭。1 行あれば「どの案か」は見分けが付く。
TEXT_HEAD = 80

KIND_LABELS = {
    media_providers.KIND_IMAGE: "絵",
    media_providers.KIND_AUDIO: "音",
    media_providers.KIND_VIDEO: "動画",
    media_providers.KIND_SPEECH: "読み上げ",
    media_providers.KIND_TEXT: "文章",
}


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind or "?")


def group_url(key: str, action: str = "") -> str:
    """組の URL。**percent-encode してから HTML のエスケープを通す**。

    組の名前は人が付けるので、`#` や `&` や `/` がそのまま入りうる ——
    素で埋めると別の組を指すか、URL が途中で切れる。
    `quote` は percent-encode であって HTML のエスケープではない(別物)。
    """
    return esc("/admin/media/" + quote(key, safe="") + action)


def _when(raw: str) -> str:
    """人に見せる日時。**書式は `app/jst.py` に集める**(画面ごとに書くと表記が割れる)。"""
    at = parse_jst(raw)
    return format_jst(at) if at else (raw or "")


def _who(job: dict) -> str:
    """作った相手。**持ち込んだものはそうと分かるようにする** ——
    出来を比べる場所なので出どころでは弾かないが、出どころは読めたほうがよい。"""
    backend = (job.get("backend") or "").strip()
    if backend == media.UPLOAD_BACKEND:
        who = "手元から持ち込み"
    else:
        spec = media_providers.get(backend)
        who = spec.label if spec else (backend or "?")
    model = (job.get("model") or "").strip()
    # **頼んだ側も並べる。** 相手とモデルだけでは「これは自分が頼んだものか」が
    # 読めない —— 同じ表に外のアプリと無人で回る層のぶんが混ざって並ぶため
    lines = [esc(who)]
    if model:
        lines.append(esc(model))
    if by := (job.get("requested_by") or "").strip():
        lines.append(f"依頼元: {esc(by)}")
    head, *rest = lines
    tail = "".join(f'<br><span class="muted">{line}</span>' for line in rest)
    return head + tail


def _text_of(job: dict) -> str:
    """文章の案の中身。**置き場から読む**(job の列には入っていない)。

    読めなければ空を返す —— 掃除で消えた後の組を開いただけで画面が落ちては困る。
    """
    for file in job.get("files") or []:
        path = (file or {}).get("path")
        if not path:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except (OSError, UnicodeDecodeError) as e:
            log.warning("text job %s unreadable: %r", job.get("id"), e)
    return ""


def _modal(anchor: str, back: str, title: str, body: str) -> str:
    """全画面で見るための覆い。**JS を持たずに開閉する**(CSS の `:target`)。

    `#<anchor>` へ飛ぶと出て、別の `#` へ移ると消える。**閉じる先は開いた側の
    カード**(`back`)—— `#` だけに戻すとページの先頭へ飛び、どれを見ていたのか
    分からなくなる。

    背景にも閉じるリンクを敷く。 覆いの外を押したら閉じる、は JS 無しでもこれで作れる。
    """
    return (
        f'<div class="media-modal" id="{esc(anchor)}">'
        f'<a class="media-modal-back" href="#{esc(back)}" aria-label="閉じる"></a>'
        f'<div class="media-modal-body">'
        f'<div class="media-modal-bar"><span>{esc(title)}</span>'
        f'<a class="media-modal-close" href="#{esc(back)}">閉じる</a></div>'
        f"{body}</div></div>"
    )


def _preview(job: dict) -> str:
    """案そのものの見せ方。**種類ごとに違うのはここだけ**。

    **絵と文章は押すと全画面になる**(`_modal`)。並べて比べるには小さくないと
    いけないが、その大きさでは絵の細部も文章の中身も読めない —— 同じ画面に
    両方を求めると、どちらも中途半端になる。
    """
    kind = job.get("kind") or ""
    files = job.get("files") or []
    if job.get("state") != "done" or not files:
        state = job.get("state") or "?"
        if error := (job.get("error") or "").strip():
            return f'<p class="stale">{esc(error)}</p>'
        return f'<p class="muted">{esc(state)}</p>'

    job_id = job.get("id") or ""
    back = f"card-{job_id}"
    title = (job.get("prompt") or "").strip().splitlines()
    title = (title[0] if title else "")[:60]

    if kind == media_providers.KIND_TEXT:
        body = _text_of(job).strip()
        if not body:
            return '<p class="muted">中身を読めなかった(掃除で消えた可能性がある)</p>'
        head = body.replace("\n", " ")[:TEXT_HEAD]
        if len(body) > TEXT_HEAD:
            head += "…"
        anchor = f"full-{job_id}"
        return (
            f'<a class="media-open" href="#{esc(anchor)}">{esc(head)}</a>'
            f'<p class="muted">{len(body):,} 字 ・ 押すと全画面で読める</p>'
            + _modal(anchor, back, title,
                     f'<pre class="media-full-text">{esc(body)}</pre>')
        )

    out = []
    for index, file in enumerate(files):
        url = esc((file or {}).get("url") or "")
        if not url:
            continue
        if kind == media_providers.KIND_IMAGE:
            anchor = f"full-{job_id}-{index}"
            out.append(
                f'<a class="media-open" href="#{esc(anchor)}">'
                f'<img class="media-img" src="{url}" alt=""></a>'
                + _modal(anchor, back, title,
                         f'<img class="media-full-img" src="{url}" alt="">'
                         f'<p class="muted"><a href="{url}">元のファイルを開く</a></p>')
            )
        elif kind == media_providers.KIND_VIDEO:
            out.append(f'<video class="media-img" src="{url}" controls preload="none"></video>')
        else:
            # 音と読み上げ。**ブラウザ内蔵のプレイヤーに任せる**(JS を持たない)
            out.append(f'<audio src="{url}" controls preload="none"></audio>')
        if seconds := (file or {}).get("seconds"):
            out.append(f'<p class="muted">{float(seconds):.1f} 秒</p>')
    return "".join(out) or '<p class="muted">中身が無い</p>'


# 元にしたものの使い道。**言葉にして出す** —— `edit` / `reference` のままだと、
# 「直した」のか「参考にした」のかが読み手に伝わらない(結果の読み方が変わる)
SOURCE_LABELS = {"edit": "これを直した", "reference": "これを参考にした"}


def _source_block(job: dict) -> str:
    """**元にしたものを、依頼文と並べて出す。**

    依頼文だけでは、出来上がりを読めないことがある —— 参考にしたものの欠点を
    そのまま引き継いだ生成物を前に、「指示が悪いのか、参考が悪いのか」を
    切り分けられなかった。元にしたものが見えれば、そこで分かる。

    **種類は job の kind で決める。** 絵の参考は絵、曲の参考は音、と揃うので、
    出来上がりと同じ見せ方でよい。
    """
    urls = [u for u in (job.get("source_url") or "").split("\n") if u.strip()]
    if not urls:
        return ""
    label = SOURCE_LABELS.get(job.get("source_mode") or "", "これを元にした")
    if len(urls) > 1:
        # **何枚目かを出す。** 役割は依頼文の中で「1 枚目は姿勢」のように書かれるので、
        # 番号が合っていないと、どれがどの役だったのか読み手が辿れない
        label += f"（{len(urls)} 枚）"
    kind = job.get("kind") or ""
    blocks = []
    for i, url in enumerate(urls, 1):
        url = url.strip()
        if kind == media_providers.KIND_VIDEO:
            view = f'<video class="media-img" src="{esc(url)}" controls preload="none"></video>'
        elif kind in (media_providers.KIND_AUDIO, media_providers.KIND_SPEECH):
            view = f'<audio src="{esc(url)}" controls preload="none"></audio>'
        else:
            view = f'<img class="media-img" src="{esc(url)}" alt="">'
        nth = f"{i} 枚目: " if len(urls) > 1 else ""
        blocks.append(
            f'<div class="media-source">{view}'
            f'<p class="muted">{esc(nth)}<a href="{esc(url)}">元のファイルを開く</a></p></div>'
        )
    return (
        f'<details class="media-text"><summary>{esc(label)}</summary>'
        + "".join(blocks) + "</details>"
    )


def _pager(page: int, has_next: bool, limit: int) -> str:
    """前後の頁へのリンク。**JS を持たないのでクエリで送る**(画面全体と同じ流儀)。

    最後の頁が何番かは出さない —— 総数を数えるには全件を束ね直すことになる。
    """
    def link(target: int, label: str, enabled: bool) -> str:
        if not enabled:
            return f'<span class="muted">{label}</span>'
        q = f"?page={target}" + (f"&limit={limit}" if limit != GROUPS_PER_PAGE else "")
        return f'<a href="/admin/media{q}">{label}</a>'

    return (
        f'<p class="muted">{link(page - 1, "← 新しい", page > 1)}'
        f'　{page} 頁目　'
        f'{link(page + 1, "古い →", has_next)}</p>'
    )


@router.get("/admin/media", response_class=HTMLResponse)
def admin_media(
    _request: Request,
    limit: int = Query(GROUPS_PER_PAGE, ge=1, le=100),
    page: int = Query(1, ge=1),
):
    """組の一覧。**中身は運ばない**(見出し・日時・種類・件数まで)。

    **遡れるようにしてある。** 新しい 20 組だけを出していた頃は、それより前が
    まだ残っているのに手が届かず、掃除で消えたものと区別が付かなかった
    (置き場の掃除は `media.KEEP_DAYS` で、そちらとは別の話)。
    """
    from app.views.admin import nav_html

    if not media.is_enabled():
        body = f"""
{nav_html("/admin/media")}
<h1>見比べ</h1>
<p class="muted">生成物の置き場が無いので、見比べるものがありません。
書き込み可能なディレクトリを <code>CHIEZO_MEDIA_DIR</code>
(または <code>CHIEZO_STATE_DIR</code>)に設定すると使えるようになります。</p>
"""
        return HTMLResponse(content=page_shell("見比べ", body))

    # **1 組ぶん多く引いて、次があるかを見る。** 総数を数えるには全件を束ね直す
    # ことになるので、「次の頁があるか」だけ分かれば足りる形にする
    offset = (page - 1) * limit
    found = media.job_groups(limit + 1, offset)
    groups, has_next = found[:limit], len(found) > limit
    if not groups:
        rows = (
            '<p class="muted">まだ何もありません。'
            '何案か作らせるときに同じ <code>group</code> を付けると、ここに 1 組として並びます。</p>'
        )
    else:
        cells = "".join(
            f'<tr><td><a href="{group_url(g["key"])}">{esc(g["title"])}</a></td>'
            f'<td>{esc(kind_label(g["kind"]))}</td>'
            f'<td>{g["count"]} 案</td>'
            f'<td>{"選んだ" if g["picked"] else "<span class=\'muted\'>まだ</span>"}</td>'
            f'<td class="muted">{esc(_when(g["created_at"]))}</td></tr>'
            for g in groups
        )
        rows = (
            "<table><thead><tr><th>見出し</th><th>種類</th><th>案</th>"
            f"<th>採用</th><th>頼んだ日時</th></tr></thead><tbody>{cells}</tbody></table>"
        )

    body = f"""
{nav_html("/admin/media")}
<h1>見比べ</h1>
<p class="muted">
何案か作らせたものを、依頼文つきで並べて選ぶところ。
<strong>音は AI 自身が聴けない</strong>ので、聴き比べる手段はここにしかありません。
選んだ印は頼んだ側が <code>GET /v1/media/picks</code> で引きに来ます
——<strong>会話で「何番がいい?」と聞かれても答えないこと</strong>。
選ぶ人と頼んだ側が別のやり取りにいると拾えないので、印はこの画面で付けます。
</p>
{rows}
{_pager(page, has_next, limit)}
<h2>手元で作ったものを並べる</h2>
<p class="muted">
生成させたものだけでなく、<strong>手元で仕上げたものや別の道具で作ったものも持ち込めます</strong>
(<code>POST /v1/media/upload</code>)。比べたいのは出どころではなく出来のほうなので、
出どころでは弾きません。同じ <code>group</code> を付ければ同じ組に並びます。
</p>
<pre class="media-body">curl -s "{esc("<このサーバー>")}/v1/media/upload" \\
  -F "file=@案1.png" -F "prompt=案1" -F "group=タイトルの一枚絵"</pre>
"""
    return HTMLResponse(content=page_shell("見比べ", body))


@router.get("/admin/media/{key:path}", response_class=HTMLResponse)
def admin_media_group(_request: Request, key: str):
    """組 1 つ。案を並べて、それぞれに印を付けられる。"""
    from app.views.admin import nav_html

    group = media.job_group(key) if media.is_enabled() else None
    if group is None:
        body = f"""
{nav_html("/admin/media")}
<h1>見比べ</h1>
<p class="muted">この組は見つかりませんでした(掃除で消えたか、名前が違う)。</p>
<p><a href="/admin/media">→ 一覧へ戻る</a></p>
"""
        return HTMLResponse(content=page_shell("見比べ", body), status_code=404)

    pick_url = group_url(key, "/pick")
    unpick_url = group_url(key, "/unpick")
    cards = []
    for job in group["jobs"]:
        picked = bool(job.get("picked_at"))
        note = (job.get("picked_note") or "").strip()
        if picked:
            mark = (
                '<p class="media-picked">これを採用'
                + (f'<br><span class="muted">{esc(note)}</span>' if note else "")
                + "</p>"
                f'<form method="post" action="{unpick_url}">'
                f'<input type="hidden" name="job_id" value="{esc(job["id"])}">'
                '<button type="submit">印を外す</button></form>'
            )
        else:
            # **1 組から選ばれるのは 1 つ。** 選び直すと前の印は外れる(`media.pick_job`)
            mark = (
                f'<form method="post" action="{pick_url}">'
                f'<input type="hidden" name="job_id" value="{esc(job["id"])}">'
                '<input type="text" name="note" placeholder="ひとこと(任意)" maxlength="200">'
                '<button type="submit">これを採用</button></form>'
            )
        cards.append(
            f'<div class="media-card{" picked" if picked else ""}"'
            f' id="card-{esc(job["id"])}">'
            f"{_preview(job)}"
            f'<p class="muted">{_who(job)}</p>'
            f'<details class="media-text"><summary>依頼文</summary>'
            f'<pre class="media-body">{esc(job.get("prompt") or "")}</pre></details>'
            f"{_source_block(job)}"
            f"{mark}</div>"
        )

    body = f"""
{nav_html("/admin/media")}
<p><a href="/admin/media">← 見比べの一覧</a></p>
<h1>{esc(group["title"])}</h1>
<p class="muted">{esc(kind_label(group["kind"]))} / {group["count"]} 案 /
{esc(_when(group["created_at"]))}</p>
<div class="media-grid">{"".join(cards)}</div>
"""
    return HTMLResponse(content=page_shell(group["title"], body))


@router.post("/admin/media/{key:path}/pick")
def admin_media_pick(key: str, job_id: str = Form(...), note: str = Form("")):
    media.pick_job(job_id, note.strip())
    return RedirectResponse("/admin/media/" + quote(key, safe=""), status_code=303)


@router.post("/admin/media/{key:path}/unpick")
def admin_media_unpick(key: str, job_id: str = Form(...)):
    media.unpick_job(job_id)
    return RedirectResponse("/admin/media/" + quote(key, safe=""), status_code=303)
