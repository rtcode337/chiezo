"""管理画面の「話す相手」節（一覧の描画と、on/off・認証情報の受け口）。

設定は環境変数ではなくここから入れる。 URL と表示名は `app/providers.py` に
決め打ちしてあり、ユーザーが決めるのは on/off・認証情報・既定のモデルだけ。
保存先は `app/settings_store.py`（`state/settings.db`）。

on にできる条件を画面側でも守る:

- 認証情報の要る相手… 未登録なら on にできない
- CLI ブリッジ（Claude Code / Codex CLI）… コンテナが立っていなければ on にできない
  （compose のコメントを外していなければ立っていない。立っていない相手を有効にしても
  会話のたびに失敗するだけなので、押させない）

どちらも `app/answer.py` 側でも弾く（設定を直に書き換えられた場合の保険）。
"""
from __future__ import annotations

from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import answer, capabilities, media, media_providers, providers, settings_store
from app.pages import CHAT_PATH, esc, markup

router = APIRouter()


def _rows() -> list[dict]:
    """一覧に出す 1 行ぶんずつ。

    画面を描くときに相手へ問い合わせない。 以前は毎回ブリッジの到達確認をしていたが、
    立っていない相手の数だけ表示が遅れるうえ、「到達できる」と「話せる」は別物だった
    （認証情報が間違っていても到達はする）。いまは「接続を試す」が通った記録だけを見る。
    """
    stored = settings_store.load_all()

    rows = []
    for spec in providers.all_providers():
        st = stored.get(spec.id, settings_store.ProviderSetting(spec.id))
        blocked = ""
        if spec.credential == providers.CRED_REQUIRED and not st.has_credential:
            blocked = "認証情報が未登録"
        elif not st.verified:
            blocked = "未確認（接続を試す）"
        rows.append(
            {
                "spec": spec,
                "enabled": st.enabled,
                "has_credential": st.has_credential,
                # 認証情報の欄を出すか（要る相手と、任意で入れられる相手）
                "takes_credential": spec.credential != providers.CRED_NONE,
                # 認証情報が無いと on にできないか（任意の相手は無くても on にできる）
                "credential_required": spec.credential == providers.CRED_REQUIRED,
                "model": st.model,
                "updated_at": st.updated_at[:19],
                # 「接続を試す」が通った記録。これが on を押せる条件
                "verified": st.verified,
                "verified_at": st.verified_at[:19],
                # can_enable が偽なら on のボタンを押させない
                "can_enable": not blocked,
                "blocked": blocked,
                "runnable": st.enabled and not blocked,
            }
        )
    return rows


SECTION_ANCHOR = "ai-providers"

# 「接続を試す」の結果は節の中に出るので、戻り先にこの印を付ける。
# 付けないとページの先頭へ戻され、結果が画面外のままになる（実際に読めなかった）。
BACK_TO_SECTION = f"/admin#{SECTION_ANCHOR}"


# kind → 分類。音だけここに載らない（1 つの kind が音楽と SE に割れるため、
# `sounds` を見て数える必要がある）。
_CAP_OF_KIND = {
    media_providers.KIND_IMAGE: capabilities.IMAGE,
    media_providers.KIND_VIDEO: capabilities.VIDEO,
    media_providers.KIND_SPEECH: capabilities.SPEECH,
    media_providers.KIND_TRANSCRIBE: capabilities.TRANSCRIBE,
}

# 相手ごとに引く kind。音も含めた全部（上の表と違い、こちらは引く対象の一覧）。
_MEDIA_KINDS = (*_CAP_OF_KIND, media_providers.KIND_AUDIO)


def _capabilities_cell(talk: dict | None, media_entries: dict[str, dict]) -> str:
    """その相手で何ができて、何ができないかを 1 つの欄にまとめる。

    印は `✓`（使える）/ `⚠`（受け持つが、いまは使えない）/ `✗`（そもそも作れない）。
    絵文字は使わない —— 環境によっては豆腐になり、いちばん見たい列が読めなくなる。

    できないことも並べる。 「書いていない = できない」は読み手に伝わらず、
    毎回ほかの行と見比べることになる。分類の数だけ並べるので、
    順番は `capabilities.CAPABILITIES` と揃える（一覧と行で並びが違うと読み比べられない）。
    """
    lines = []

    label = capabilities.BY_ID[capabilities.CHAT].label
    if talk is None:
        lines.append(f'<span class="muted">✗ {label}</span>')
    elif talk["runnable"]:
        lines.append(f"✓ {label}")
    else:
        why = talk["blocked"] or ("無効" if not talk["enabled"] else "")
        lines.append(f'⚠ {label} <span class="muted">{esc(why)}</span>' if why else f"⚠ {label}")

    for cap_id, kind in (
        (capabilities.SPEECH, media_providers.KIND_SPEECH),
        (capabilities.TRANSCRIBE, media_providers.KIND_TRANSCRIBE),
        (capabilities.IMAGE, media_providers.KIND_IMAGE),
        (capabilities.VIDEO, media_providers.KIND_VIDEO),
    ):
        lines.append(_media_line(cap_id, media_entries.get(kind), models=True))

    audio = media_entries.get(media_providers.KIND_AUDIO)
    sounds = audio.get("sounds", {}) if audio else {}
    for cap_id, sound in ((capabilities.MUSIC, media_providers.SOUND_MUSIC),
                          (capabilities.SFX, media_providers.SOUND_SFX)):
        lines.append(_media_line(cap_id, audio if sound in sounds else None,
                                 limit=sounds.get(sound)))

    return "<br>".join(lines)


def _media_line(cap_id: str, entry: dict | None, models: bool = False,
                limit: float | None = None) -> str:
    """絵・音の 1 行。`entry` が無ければその相手は作れない（✗）。

    使える相手は理由の代わりに手掛かり（モデル名・長さの上限）を、
    使えない相手は理由をそのまま出す（次にすることが分かるように）。
    """
    label = capabilities.BY_ID[cap_id].label
    if entry is None:
        return f'<span class="muted">✗ {label}</span>'
    if not entry["usable"]:
        return f'⚠ {label} <span class="muted">{esc(entry["reason"])}</span>'
    tail = ""
    if models and entry["models"]:
        tail = f' <span class="muted">{esc("、".join(entry["models"][:2]))}</span>'
    elif models and entry.get("voices"):
        # モデル名を持たない相手（声を相手に聞く ElevenLabs）は、代わりに声の数を出す
        tail = f' <span class="muted">声 {len(entry["voices"])} 件</span>'
    elif limit:
        tail = f' <span class="muted">〜{limit:.0f} 秒</span>'
    return f"✓ {label}{tail}"


def _overview_html(usable: dict[str, set[str]]) -> str:
    """表の上に、頼めることの一覧を出す。 行ごとの欄は「その相手で何ができるか」しか
    示さないので、そもそも何を頼めるのか（と、まだ頼めないもの）はここで見せる。"""
    cells = []
    for item in capabilities.overview(usable):
        if item["state"] == "使える":
            names = "、".join(esc(capabilities.label_of(p)) for p in item["providers"])
            body = f'<strong>✓ {esc(item["label"])}</strong><br><span class="muted">{names}</span>'
        elif item["state"] == "相手がいない":
            body = (f'<strong>{esc(item["label"])}</strong>'
                    '<br><span class="muted">使える相手がいない</span>')
        else:
            body = f'<span class="muted">{esc(item["label"])}<br>未対応</span>'
        cells.append(f"<td>{body}</td>")
    return (
        '<table class="ai-settings"><thead><tr><th colspan="6">'
        "chiezo 経由で AI に頼めること</th></tr></thead>"
        f"<tbody><tr>{''.join(cells)}</tr></tbody></table>"
    )


def _talk_cells(r: dict) -> tuple[str, str]:
    """「話す相手」側の認証情報の欄と操作の欄。"""
    spec = r["spec"]
    if not r["takes_credential"]:
        # 「要らない」のではなく「渡すものが無い」。 Antigravity は API キー方式も
        # 持たず、コンテナ内で 1 回サインインした結果を使う。ここで「不要」とだけ書くと
        # 何もしなくてよいと読めてしまうので、何をすればよいかを添える。
        cred = (
            '<span class="muted">渡すものが無い</span>'
            f'<details><summary>使えるようにするには</summary>'
            f'<p class="muted">{markup(spec.setup)}</p></details>'
        )
    elif not r["credential_required"]:
        cred = "登録済み" if r["has_credential"] else "未登録"
        cred += '<br><span class="muted">認証を掛けているときだけ</span>'
    elif r["has_credential"]:
        cred = (
            f"登録済み<br><span class=\"muted\">{esc(r['updated_at'])}</span>"
            f'<form method="post" action="/admin/ai/key" class="init-form"'
            f" onsubmit=\"return confirm('認証情報を削除して無効にします。よろしいですか?')\">"
            f'<input type="hidden" name="provider" value="{spec.id}">'
            f'<input type="hidden" name="action" value="delete">'
            f"<button type=\"submit\">削除</button></form>"
        )
    else:
        cred = "未登録"

    # 入力欄は details に畳む。常設すると、鍵の要る相手の数だけ表が縦に伸びる。
    if r["takes_credential"]:
        cred += (
            f"<details><summary>{'更新' if r['has_credential'] else '登録'}する</summary>"
            f'<p class="muted">{markup(spec.setup)}</p>'
            f'<form method="post" action="/admin/ai/key">'
            f'<input type="hidden" name="provider" value="{spec.id}">'
            f'<input type="text" name="credential" placeholder="認証情報" required>'
            f"<button type=\"submit\">保存</button></form>"
            f'<p class="muted">保存しても有効にはなりません(有効化は右のボタン)。'
            f"値は画面に二度と表示しません。</p></details>"
        )

    use = (
        f'<form method="post" action="/admin/ai/enabled" class="init-form">'
        f'<input type="hidden" name="provider" value="{spec.id}">'
        f'<input type="hidden" name="enabled" value="{"0" if r["enabled"] else "1"}">'
        f"<button type=\"submit\"{'' if (r['enabled'] or r['can_enable']) else ' disabled'}>"
        f"{'無効にする' if r['enabled'] else '有効にする'}</button></form>"
        f'<form method="post" action="/admin/ai/test" class="init-form">'
        f'<input type="hidden" name="provider" value="{spec.id}">'
        f"<button type=\"submit\">接続を試す</button></form>"
    )
    return cred, use


def _media_only_cells(spec, enabled: bool) -> tuple[str, str]:
    """話せない相手（自前の GPU・ElevenLabs）の認証情報の欄と操作の欄。

    こちらは「話す相手」に対応が無いので、鍵も on/off もここに置くしかない。
    """
    if spec.credential == media_providers.CRED_NONE:
        cred = (
            '<span class="muted">渡すものが無い</span>'
            f'<details><summary>使えるようにするには</summary>'
            f'<p class="muted">{markup(spec.setup)}</p></details>'
        )
    else:
        has = bool(settings_store.load(spec.id).credential)
        drop = (
            f'<form method="post" action="/admin/media/key" class="init-form">'
            f'<input type="hidden" name="provider" value="{spec.id}">'
            f'<input type="hidden" name="action" value="delete">'
            f"<button type=\"submit\">削除</button></form>"
        ) if has else ""
        cred = "登録済み" if has else "未登録"
        cred += (
            f"<details><summary>{'更新' if has else '登録'}する</summary>"
            f'<p class="muted">{markup(spec.setup)}</p>'
            f'<form method="post" action="/admin/media/key">'
            f'<input type="hidden" name="provider" value="{spec.id}">'
            f'<input type="password" name="credential" placeholder="API キー" required>'
            f"<button type=\"submit\">保存</button></form>{drop}"
            f'<p class="muted">値は画面に二度と表示しません。</p></details>'
        )

    use = (
        f'<form method="post" action="/admin/media/enabled" class="init-form">'
        f'<input type="hidden" name="provider" value="{spec.id}">'
        f'<input type="hidden" name="enabled" value="{"0" if enabled else "1"}">'
        f'<button type="submit">{"無効にする" if enabled else "有効にする"}</button></form>'
        f'<form method="post" action="/admin/media/test" class="init-form">'
        f'<input type="hidden" name="provider" value="{spec.id}">'
        f"<button type=\"submit\">接続を試す</button></form>"
    )
    return cred, use


async def section_html(request: Request | None = None) -> str:
    """管理画面に差し込む「AI の相手」節。

    見た目は管理画面の素っ気なさに合わせる（表と details だけ。JS も持たない）。
    会話画面は毎日触るので作り込んであるが、こちらは設定を一度入れたら開かない場所である。

    話す相手と、絵や音を作る相手を 1 つの表にまとめてある。 分けていた頃は
    同じ相手（鍵も on/off も共通）が 2 か所に出ていて、どちらが効くのか読めなかった。
    """
    if not settings_store.is_enabled():
        return (
            f'<h2 id="{SECTION_ANCHOR}">AI の相手</h2>\n'
            '<p class="muted">設定の保存先がありません。書き込み可能なディレクトリを'
            " <code>CHIEZO_STATE_DIR</code> に設定すると、ここから相手を追加できます"
            "(compose では <code>./state:/state</code> をマウント済み)。</p>"
        )

    # 「接続を試す」の結果。画面に残すのは 1 回だけ（リロードで消える）ので、
    # クエリで受け渡す —— セッションを持たない作りに合わせる。
    banner = ""
    q = request.query_params if request is not None else {}
    if tested := q.get("tested"):
        label = esc(providers.label_of(tested))
        if q.get("ok") == "1":
            banner = f'<p class="note">✅ {label} と話せます。</p>'
        else:
            banner = f'<p class="stale">⚠️ {label} と話せません: {esc(q.get("why", ""))}</p>'
    elif tested := q.get("media_tested"):
        label = esc(media_providers.label_of(tested))
        if q.get("media_ok") == "1":
            banner = f'<p class="note">✅ {label} と繋がりました: {esc(q.get("media_why", ""))}</p>'
        else:
            banner = f'<p class="stale">⚠️ {label} と繋がりません: {esc(q.get("media_why", ""))}</p>'

    on = settings_store.answer_enabled()
    master = (
        '<div class="job-status">'
        f'<strong>「答える」層: {"有効" if on else "停止中"}</strong> '
        '<form method="post" action="/admin/ai/layer" class="init-form">'
        f'<input type="hidden" name="enabled" value="{"0" if on else "1"}">'
        f'<button type="submit">{"停止する" if on else "有効にする"}</button></form>'
        '<p class="muted">元栓。止めると、下で有効にしてある相手があっても'
        " <code>/v1/ask</code>・<code>/ai/chat</code> は 503 になり、"
        "絵と音の道具も出なくなる"
        "(相手を 1 つずつ切って回らずに、機能ごと止めたいとき用)。</p></div>"
    )

    # kind ごとに引いて相手 ID で束ねる。 一覧を混ぜると頼めない相手が並んで見える
    by_id: dict[str, dict[str, dict]] = {}
    if media.is_enabled():
        for kind in _MEDIA_KINDS:
            for entry in await media.backends(kind):
                by_id.setdefault(entry["id"], {})[kind] = entry

    rows = []
    talk_ids = set()
    usable: dict[str, set[str]] = {}
    for r in _rows():
        spec = r["spec"]
        talk_ids.add(spec.id)
        cred, use = _talk_cells(r)
        if r["runnable"]:
            usable.setdefault(spec.id, set()).add(capabilities.CHAT)
        rows.append(
            f'<tr{"" if r["enabled"] else ' class="off"'}><td>{esc(spec.label)}</td>'
            f'<td>{_capabilities_cell(r, by_id.get(spec.id, {}))}</td>'
            f"<td>{cred}</td><td>{use}</td>"
            f'<td class="muted">{esc(spec.billing)}</td></tr>'
        )

    # 絵・音・動画・声は「いま使えるか」を相手ごとに数える（上の一覧に渡す）。
    # 音だけ 1 つの kind が 2 つの分類に割れるので、そこだけ別に数える。
    for pid, kinds in by_id.items():
        for kind, entry in kinds.items():
            if not entry["usable"]:
                continue
            if cap_id := _CAP_OF_KIND.get(kind):
                usable.setdefault(pid, set()).add(cap_id)
                continue
            for cap_id, sound in ((capabilities.MUSIC, media_providers.SOUND_MUSIC),
                                  (capabilities.SFX, media_providers.SOUND_SFX)):
                if sound in entry.get("sounds", {}):
                    usable.setdefault(pid, set()).add(cap_id)

    # 話せない相手（自前の GPU・ElevenLabs）は「話す相手」に出てこないので、続けて並べる
    for spec in media_providers.all_providers():
        if spec.id in talk_ids or spec.credential_from:
            continue
        enabled = settings_store.load(spec.id).enabled
        cred, use = _media_only_cells(spec, enabled)
        rows.append(
            f'<tr{"" if enabled else ' class="off"'}><td>{esc(spec.label)}</td>'
            f'<td>{_capabilities_cell(None, by_id.get(spec.id, {}))}</td>'
            f"<td>{cred}</td><td>{use}</td>"
            f'<td class="muted">{esc(spec.billing)}</td></tr>'
        )

    media_note = "" if media.is_enabled() else (
        '<p class="stale">⚠️ 出来たものの置き場がないので、絵と音は作れません。'
        " 書き込み可能なディレクトリを <code>CHIEZO_MEDIA_DIR</code>"
        "(既定は <code>CHIEZO_STATE_DIR</code> の下)に設定してください。</p>"
    )

    return f"""<h2 id="{SECTION_ANCHOR}">AI の相手</h2>
{banner}
{master}
{media_note}
{_overview_html(usable)}
<details>
<summary>この節について</summary>
<p>Chiezo にためた知識を引ける AI と、絵や音を作る相手をここで増やす。
<strong>相手の URL は決まっているので設定に出さない</strong>
(<code>app/providers.py</code> と <code>app/media_providers.py</code> に決め打ち)
—— 入れるのは認証情報と、使うかどうかだけ。</p>
<p><strong>1 つの相手で複数のことができる。</strong>「できること」の欄が、その相手で
いま何ができるか（話す・絵・音）を示す。<strong>鍵と on/off は相手ごとに 1 つ</strong>なので、
無効にすると全部止まる —— 同じものを 2 か所から切り替えさせない。</p>
<p>どのモデルを使うかは<strong>会話のたびに選べる</strong>(<a href="{CHAT_PATH}">AI と話す</a>の画面)。</p>
<p><strong>Claude Code CLI / Codex CLI / Antigravity CLI は CLI なので、別コンテナ(ブリッジ)を
立てて使う。</strong>立てるとここが押せるようになる（手順は各行の「登録する」「使えるようにするには」の中）。
<strong>compose のファイルが無い環境でも立てられる</strong> —— <code>docker run</code> で、
<strong>コンテナ名を <code>chiezo-bridge-&lt;CLI 名&gt;</code> にして chiezo-app と同じネットワークに繋ぐ</strong>
のが条件（この名前で呼びに行くため）。<strong>認証情報はこの画面から登録する</strong> —— ブリッジが設定 DB を
読み取り専用でマウントして読むので、登録すればブリッジの再起動なしで効く
（<strong>Antigravity だけは例外</strong>で、API キーを持たずコンテナ内で 1 回サインインする）。</p>
<p><strong>自前の GPU(ComfyUI)は「話す相手」ではない。</strong>
<code>docker-compose.image.yml</code> を重ねて立てるか、別マシンのものを
<code>CHIEZO_IMAGE_URL</code> で指す(compose が無ければ後者が早い)。<strong>モデルは自分で置く</strong>
—— 絵と音で別のファイルが要る(置き場は <code>docs/ai.md</code>)。</p>
<p class="stale">⚠️ Chiezo は認証なし・LAN 内前提。ここに入れた認証情報は、この画面を開ける人なら
誰でも差し替えられる(値は表示しないが、書き換えは防げない)。</p>
</details>
<table class="ai-settings">
<thead><tr><th>AI</th><th>できること</th><th>認証情報</th><th>使う</th><th>課金の形</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody>
</table>
"""


def _require_provider(provider: str) -> providers.Provider:
    spec = providers.get(provider)
    if spec is None:
        raise HTTPException(404, {"error": f"unknown provider: {provider}"})
    return spec


@router.post("/admin/ai/key")
async def set_credential(provider: str = Form(...), credential: str = Form(""), action: str = Form("")):
    """認証情報の登録・削除。

    削除を同じ入口にまとめてあるのは、認証情報を消したら同時に無効にする必要があるため
    （認証情報の無い相手を有効のまま残すと、会話のたびに失敗するだけになる）。
    """
    spec = _require_provider(provider)
    settings_store.require_path()
    if spec.credential == providers.CRED_NONE:
        raise HTTPException(400, {"error": f"「{spec.label}」は認証情報を受け取りません"})
    if action == "delete":
        settings_store.clear_credential(spec.id)
    else:
        value = credential.strip()
        if not value:
            raise HTTPException(400, {"error": "認証情報が空です"})
        settings_store.set_credential(spec.id, value)
    return RedirectResponse(url=BACK_TO_SECTION, status_code=303)


@router.post("/admin/ai/enabled")
async def set_enabled(provider: str = Form(...), enabled: str = Form("0")):
    """on/off の切り替え。on にできない相手は on にしない（画面側の抑止の裏打ち）。"""
    spec = _require_provider(provider)
    settings_store.require_path()
    want = enabled == "1"
    if want:
        st = settings_store.load(spec.id)
        if spec.credential == providers.CRED_REQUIRED and not st.has_credential:
            raise HTTPException(400, {"error": f"先に「{spec.label}」の認証情報を登録してください"})
        # 「接続を試す」が一度でも通っていないと on にできない。 到達できるだけでは
        # 話せる保証にならず（認証情報が間違っていても到達はする）、会話して初めて失敗する。
        if not st.verified:
            raise HTTPException(
                400,
                {"error": f"先に「{spec.label}」の「接続を試す」を通してください", "hint": spec.setup},
            )
    settings_store.set_enabled(spec.id, want)
    return RedirectResponse(url=BACK_TO_SECTION, status_code=303)


def _require_media_owner(provider: str) -> media_providers.MediaProvider:
    """自分の on/off を持つ相手だけを受け付ける。借り物の相手はここでは触らせない
    —— あちらは「話す相手」と共通で、2 か所から切れるとどちらが効いているのか
    分からなくなる。"""
    spec = media_providers.get(provider)
    if spec is None or not spec.owns_toggle:
        raise HTTPException(404, {"error": f"unknown backend: {provider}"})
    return spec


@router.post("/admin/media/enabled")
async def set_media_enabled(provider: str = Form(...), enabled: str = Form("0")):
    """自前の GPU(ComfyUI)・ElevenLabs の on/off。"""
    spec = _require_media_owner(provider)
    settings_store.require_path()
    want = enabled == "1"
    # 鍵の要る相手は、鍵が無ければ on にできない。 有効のまま残しても、
    # 頼むたびに 401 になるだけ(「話す相手」と同じ抑止)
    if (
        want
        and spec.credential == media_providers.CRED_REQUIRED
        and not settings_store.load(spec.credential_from or spec.id).credential
    ):
        raise HTTPException(400, {"error": f"先に「{spec.label}」の API キーを登録してください"})

    settings_store.set_enabled(spec.id, want)
    return RedirectResponse(BACK_TO_SECTION, status_code=303)


@router.post("/admin/media/key")
async def set_media_credential(
    provider: str = Form(...), credential: str = Form(""), action: str = Form("")
):
    """「話す相手」に対応が無い相手(ElevenLabs)の鍵をここで登録・削除する。

    借り先のある相手はここへ来ない —— 同じ鍵を 2 か所に入れさせると、片方だけ古くなる。
    削除を同じ入口にまとめてあるのは「話す相手」と同じ理由で、鍵を消したら同時に
    無効にする必要があるため(鍵の無い相手を有効のまま残すと、頼むたびに失敗する)。
    """
    spec = _require_media_owner(provider)
    if spec.credential == media_providers.CRED_NONE or spec.credential_from:
        raise HTTPException(400, {"error": f"「{spec.label}」の鍵はここでは登録しません"})
    settings_store.require_path()
    if action == "delete":
        settings_store.clear_credential(spec.id)
    elif not credential.strip():
        raise HTTPException(400, {"error": "認証情報が空です"})
    else:
        settings_store.set_credential(spec.id, credential.strip())
    return RedirectResponse(BACK_TO_SECTION, status_code=303)


@router.post("/admin/media/test")
async def test_media_connection(provider: str = Form(...)):
    """絵と音を作る相手と実際に話せるか確かめる(結果はクエリで画面へ返す)。"""
    ok, why = await media.check(provider)
    params = urlencode({"media_tested": provider, "media_ok": "1" if ok else "0", "media_why": why})
    return RedirectResponse(f"/admin?{params}#{SECTION_ANCHOR}", status_code=303)


@router.post("/admin/ai/layer")
async def set_layer(enabled: str = Form("1")):
    """「答える」層そのものの on/off（元栓）。"""
    settings_store.require_path()
    settings_store.set_answer_enabled(enabled == "1")
    return RedirectResponse(url=BACK_TO_SECTION, status_code=303)


@router.post("/admin/ai/test")
async def test_connection(provider: str = Form(...)):
    """「接続を試す」。会話は 1 往復もせず、相手に軽く聞くだけで確かめる。

    - CLI ブリッジ … ブリッジの `/health?check=1`（CLI に `claude auth status` 等を聞かせる）
    - それ以外 … OpenAI 互換の `/models` を引く

    登録の有無ではなく「いま使えるか」を見る。打ち間違えた認証情報や期限切れは前者では
    分からず、会話して初めて失敗する（本番でそれが 502 として出た）。
    """
    spec = _require_provider(provider)
    ok, why = False, ""

    if spec.bridge:
        url = providers.url_of(spec).rstrip("/")
        base = url[: -len("/v1")] if url.endswith("/v1") else url
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                res = await client.get(f"{base}/health", params={"check": "1"})
            body = res.json()
            ok, why = bool(body.get("authenticated")), str(body.get("reason") or "")
        except (httpx.HTTPError, ValueError) as e:
            ok, why = False, f"ブリッジにつながりません: {e}"
    else:
        cfg = answer.load_settings(spec.id, require_enabled=False)
        if cfg is None:
            ok, why = False, "認証情報が未登録です"
        else:
            ok, why = await answer.check_credential(cfg)

    # 結果を残す。 これが on を押せるかどうかの根拠になる。
    settings_store.set_verified(spec.id, ok)
    params = {"tested": spec.id, "ok": "1" if ok else "0"}
    if why:
        params["why"] = why[:300]
    return RedirectResponse(url=f"/admin?{urlencode(params)}#{SECTION_ANCHOR}", status_code=303)


@router.get("/ai/models")
async def list_models(request: Request, backend: str = ""):
    """会話画面がモデルとエフォートのセレクトを組み立てるために引く。

    モデルは相手に聞けたらその一覧、聞けなければ `app/providers.py` の控え。
    エフォートは聞く口が無いので控えだけ（持たない相手では空）。
    """
    name = answer.normalize_backend(backend)
    if name not in answer.backend_names():
        raise HTTPException(404, {"error": f"unknown backend: {name}"})
    spec = providers.get(name)
    return {
        "backend": name,
        "models": await answer.available_models(name),
        "efforts": list(providers.efforts_of(name)),
        # CLI ブリッジかどうか（会話画面のトグルの出し分けに使う）
        "bridge": bool(spec and spec.bridge),
    }
