"""管理画面の「話す相手」節（一覧の描画と、on/off・認証情報の受け口）。

**設定は環境変数ではなくここから入れる。** URL と表示名は `app/providers.py` に
決め打ちしてあり、ユーザーが決めるのは on/off・認証情報・既定のモデルだけ。
保存先は `app/settings_store.py`（`state/settings.db`）。

on にできる条件を画面側でも守る:

- 認証情報の要る相手… **未登録なら on にできない**
- CLI ブリッジ（Claude Code / Codex CLI）… **コンテナが立っていなければ on にできない**
  （compose のコメントを外していなければ立っていない。立っていない相手を有効にしても
  会話のたびに失敗するだけなので、押させない）

どちらも `app/answer.py` 側でも弾く（設定を直に書き換えられた場合の保険）。
"""
from __future__ import annotations

from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import answer, media, media_providers, providers, settings_store
from app.pages import CHAT_PATH, esc

router = APIRouter()


def _rows() -> list[dict]:
    """一覧に出す 1 行ぶんずつ。

    **画面を描くときに相手へ問い合わせない。** 以前は毎回ブリッジの到達確認をしていたが、
    立っていない相手の数だけ表示が遅れるうえ、「到達できる」と「話せる」は別物だった
    （認証情報が間違っていても到達はする）。いまは**「接続を試す」が通った記録**だけを見る。
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
                # 「接続を試す」が通った記録。**これが on を押せる条件**
                "verified": st.verified,
                "verified_at": st.verified_at[:19],
                # can_enable が偽なら on のボタンを押させない
                "can_enable": not blocked,
                "blocked": blocked,
                "runnable": st.enabled and not blocked,
            }
        )
    return rows


async def section_html(request: Request | None = None) -> str:
    """管理画面に差し込む「話す相手」節。

    見た目は**管理画面の素っ気なさに合わせる**（表と details だけ。JS も持たない）。
    会話画面は毎日触るので作り込んであるが、こちらは設定を一度入れたら開かない場所である。
    """
    if not settings_store.is_enabled():
        return (
            "<h2>話す相手</h2>\n"
            '<p class="muted">設定の保存先がありません。書き込み可能なディレクトリを'
            " <code>CHIEZO_STATE_DIR</code> に設定すると、ここから話す相手を追加できます"
            "(compose では <code>./state:/state</code> をマウント済み)。</p>"
        )

    # 「接続を試す」の結果。**画面に残すのは 1 回だけ**（リロードで消える）ので、
    # クエリで受け渡す —— セッションを持たない作りに合わせる。
    banner = ""
    q = request.query_params if request is not None else {}
    if tested := q.get("tested"):
        label = esc(providers.label_of(tested))
        if q.get("ok") == "1":
            banner = f'<p class="note">✅ {label} と話せます。</p>'
        else:
            banner = f'<p class="stale">⚠️ {label} と話せません: {esc(q.get("why", ""))}</p>'

    on = settings_store.answer_enabled()
    master = (
        '<div class="job-status">'
        f'<strong>「答える」層: {"有効" if on else "停止中"}</strong> '
        '<form method="post" action="/admin/ai/layer" class="init-form">'
        f'<input type="hidden" name="enabled" value="{"0" if on else "1"}">'
        f'<button type="submit">{"停止する" if on else "有効にする"}</button></form>'
        '<p class="muted">元栓。止めると、下で有効にしてある相手があっても'
        " <code>/v1/ask</code>・<code>/ai/chat</code> は 503 になる"
        "(相手を 1 つずつ切って回らずに、機能ごと止めたいとき用)。</p></div>"
    )

    rows = []
    for r in _rows():
        spec = r["spec"]
        if r["runnable"]:
            state = "使える"
        elif r["blocked"]:
            state = r["blocked"]
        else:
            state = f"確認済み（{r['verified_at'][:10]}）" if r["verified"] else "無効"

        if not r["takes_credential"]:
            # **「要らない」のではなく「渡すものが無い」。** Antigravity は API キー方式も
            # 持たず、コンテナ内で 1 回サインインした結果を使う。ここで「不要」とだけ書くと
            # 何もしなくてよいと読めてしまうので、何をすればよいかを添える。
            cred_cell = (
                '<span class="muted">渡すものが無い</span>'
                f'<details><summary>使えるようにするには</summary>'
                f'<p class="muted">{esc(spec.setup)}</p></details>'
            )
        elif not r["credential_required"]:
            cred_cell = "登録済み" if r["has_credential"] else "未登録"
            cred_cell += '<br><span class="muted">認証を掛けているときだけ</span>'
        elif r["has_credential"]:
            cred_cell = (
                f"登録済み<br><span class=\"muted\">{esc(r['updated_at'])}</span>"
                f'<form method="post" action="/admin/ai/key" class="init-form"'
                f" onsubmit=\"return confirm('認証情報を削除して無効にします。よろしいですか?')\">"
                f'<input type="hidden" name="provider" value="{spec.id}">'
                f'<input type="hidden" name="action" value="delete">'
                f"<button type=\"submit\">削除</button></form>"
            )
        else:
            cred_cell = "未登録"

        # 入力欄は details に畳む。常設すると、鍵の要る相手の数だけ表が縦に伸びる。
        if r["takes_credential"]:
            cred_cell += (
                f"<details><summary>{'更新' if r['has_credential'] else '登録'}する</summary>"
                f'<p class="muted">{esc(spec.setup)}</p>'
                f'<form method="post" action="/admin/ai/key">'
                f'<input type="hidden" name="provider" value="{spec.id}">'
                f'<input type="text" name="credential" placeholder="認証情報" required>'
                f"<button type=\"submit\">保存</button></form>"
                f'<p class="muted">保存しても有効にはなりません(有効化は右のボタン)。'
                f"値は画面に二度と表示しません。</p></details>"
            )

        test_btn = (
            f'<form method="post" action="/admin/ai/test" class="init-form">'
            f'<input type="hidden" name="provider" value="{spec.id}">'
            f"<button type=\"submit\">接続を試す</button></form>"
        )

        toggle = (
            f'<form method="post" action="/admin/ai/enabled" class="init-form">'
            f'<input type="hidden" name="provider" value="{spec.id}">'
            f'<input type="hidden" name="enabled" value="{"0" if r["enabled"] else "1"}">'
            f"<button type=\"submit\"{'' if (r['enabled'] or r['can_enable']) else ' disabled'}>"
            f"{'無効にする' if r['enabled'] else '話せるようにする'}</button></form>"
        )
        # **絵や音も作れる相手はそう分かるようにする。** 同じ鍵で 2 つの用途に使えることも、
        # ここで無効にすると絵と音のほうも止まることも、画面から読めないと事故になる
        draws = ""
        borrowers = [m for m in media_providers.all_providers() if m.credential_from == spec.id]
        if borrowers:
            what = "・".join(
                sorted({"🎨 絵" if media_providers.KIND_IMAGE in m.kinds else "" for m in borrowers}
                       | {"🎵 音" if media_providers.KIND_AUDIO in m.kinds else "" for m in borrowers}
                       - {""})
            )
            draws = (
                f'<br><span class="muted">{what}の生成にも使える'
                f"{'(無効にすると作れなくなる)' if r['enabled'] else '(有効にすると使える)'}"
                "</span>"
            )

        # 手順は鍵の欄の details に出ているので、ここには繰り返さない
        # （同じ長文が 1 行に 2 回出て、表が読めない高さになる）。
        rows.append(
            f"<tr><td>{esc(spec.label)}{draws}</td><td>{esc(state)}</td>"
            f"<td>{cred_cell}</td><td>{toggle}{test_btn}</td>"
            f'<td class="muted">{esc(spec.billing)}</td></tr>'
        )

    return f"""<h2>話す相手</h2>
{banner}
{master}
<details>
<summary>この節について</summary>
<p>Chiezo にためた知識を引ける AI をここで増やす。<strong>相手の URL は決まっているので設定に出さない</strong>
(<code>api/app/providers.py</code> に決め打ち)—— 入れるのは認証情報と、使うかどうかだけ。</p>
<p>どのモデルを使うかは<strong>会話のたびに選べる</strong>(<a href="{CHAT_PATH}">AI と話す</a>の画面)。</p>
<p><strong>Claude Code / Codex CLI は CLI なので、別コンテナ(ブリッジ)を立てて使う。</strong>
<code>docker-compose.yml</code> の該当サービスのコメントを外して起動すると、ここが押せるようになる。
<strong>認証情報はこの画面から登録する</strong> —— ブリッジが設定 DB を読み取り専用でマウントして
読むので、登録すればブリッジの再起動なしで効く。</p>
<p class="stale">⚠️ Chiezo は認証なし・LAN 内前提。ここに入れた認証情報は、この画面を開ける人なら
誰でも差し替えられる(値は表示しないが、書き換えは防げない)。</p>
</details>
<table class="ai-settings">
<thead><tr><th>AI</th><th>状態</th><th>認証情報</th><th>使う</th><th>課金の形</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody>
</table>
{await _media_section_html(q)}
"""


async def _media_section_html(q) -> str:
    """「絵と音を作る相手」節。**上の表とは別に出す** —— 自前の GPU(ComfyUI)や
    ElevenLabs は「話す相手」ではないので上の表に出てこず、状態を見る場所が無くなるため。

    **相手ごとに 1 行**にして、作れるもの(絵・音)は行の中に並べる —— 同じ相手が
    2 行に分かれると、on/off がどちらに効くのか読めなくなる。
    """
    banner = ""
    if tested := q.get("media_tested"):
        label = esc(media_providers.label_of(tested))
        if q.get("media_ok") == "1":
            banner = f'<p class="note">✅ {label} と繋がりました: {esc(q.get("media_why", ""))}</p>'
        else:
            banner = f'<p class="stale">⚠️ {label} と繋がりません: {esc(q.get("media_why", ""))}</p>'

    if not media.is_enabled():
        return (
            "<h2>絵と音を作る相手</h2>\n"
            '<p class="muted">出来たものの置き場がありません。書き込み可能なディレクトリを'
            " <code>CHIEZO_MEDIA_DIR</code>(既定は <code>CHIEZO_STATE_DIR</code> の下)に"
            "設定すると使えるようになります。</p>"
        )

    # **kind ごとに引く。** 一覧を混ぜると、頼めない相手が並んで見えてしまう
    entries: dict[str, dict[str, dict]] = {}
    for kind, label in ((media_providers.KIND_IMAGE, "絵"), (media_providers.KIND_AUDIO, "音")):
        for entry in await media.backends(kind):
            entries.setdefault(entry["id"], {})[label] = entry

    rows = []
    for spec in media_providers.all_providers():
        found = entries.get(spec.id, {})
        if not found:
            continue
        first = next(iter(found.values()))

        # 作れるものと、その状態。**片方だけ使えることがある**
        # (絵のチェックポイントはあるが音は置いていない、など)
        lines = []
        for label, entry in found.items():
            models = "、".join(entry["models"][:3]) or "—"
            state = f"使える({esc(models)})" if entry["usable"] else esc(entry["reason"])
            extra = ""
            if label == "音":
                extra = "、".join(
                    f"{'効果音' if sound == media_providers.SOUND_SFX else '曲'}"
                    f"{f' 〜{limit:.0f} 秒' if limit else '(長さは指定できない)'}"
                    for sound, limit in entry.get("sounds", {}).items()
                )
                extra = f'<br><span class="muted">{esc(extra)}</span>' if extra else ""
            lines.append(f"<strong>{label}</strong> — {state}{extra}")

        if spec.credential == media_providers.CRED_NONE:
            cred_cell = '<span class="muted">不要</span>'
        elif spec.credential_from:
            # 借り物。**同じ鍵を 2 か所に入れさせない**
            cred_cell = '<span class="muted">上の「話す相手」と共通</span>'
        else:
            # 会話ができない相手は借り先が無いので、ここで登録する
            has = bool(settings_store.load(spec.id).credential)
            # **消す導線も置く。** 登録しかできないと、間違えて入れた鍵を画面から
            # 外せなくなる(「話す相手」側と同じ扱い)
            drop = (
                f'<form method="post" action="/admin/media/key" class="init-form">'
                f'<input type="hidden" name="provider" value="{spec.id}">'
                f'<input type="hidden" name="action" value="delete">'
                f'<button type="submit">削除</button></form>'
            ) if has else ""
            cred_cell = (
                f"<details><summary>{'登録済み(差し替え)' if has else '未登録'}</summary>"
                f'<form method="post" action="/admin/media/key" class="init-form">'
                f'<input type="hidden" name="provider" value="{spec.id}">'
                f'<input type="password" name="credential" placeholder="API キー" required>'
                f'<button type="submit">保存</button></form>{drop}'
                f'<p class="muted">値は画面に二度と表示しません。</p></details>'
            )

        if first["owns_toggle"]:
            # 「話す相手」に出てこないので、on/off と接続確認をここに置く
            use_cell = (
                f'<form method="post" action="/admin/media/enabled" class="init-form">'
                f'<input type="hidden" name="provider" value="{spec.id}">'
                f'<input type="hidden" name="enabled" value="{"0" if first["enabled"] else "1"}">'
                f'<button type="submit">{"無効にする" if first["enabled"] else "使う"}</button></form>'
                f'<form method="post" action="/admin/media/test" class="init-form">'
                f'<input type="hidden" name="provider" value="{spec.id}">'
                f"<button type=\"submit\">接続を試す</button></form>"
            )
        else:
            # 「話す相手」に対応がある相手は鍵も on/off も共通。二重に持たない
            use_cell = '<span class="muted">上の「話す相手」で切り替える</span>'

        rows.append(
            f'<tr><td>{esc(spec.label)}</td><td>{"<br>".join(lines)}</td>'
            f"<td>{cred_cell}</td><td>{use_cell}</td>"
            f'<td class="muted">{esc(spec.billing)}</td></tr>'
        )

    return f"""<h2>絵と音を作る相手</h2>
{banner}
<details>
<summary>この節について</summary>
<p>MCP の <code>image_generate</code> / <code>audio_generate</code> で使う相手。
<strong>「話す相手」に対応がある相手は、鍵も on/off も上の表と共通</strong>
(同じ鍵を 2 か所に入れさせないため)—— 上で無効にすると絵も音も作れなくなる。</p>
<p>自前の GPU(ComfyUI)と ElevenLabs は「話す相手」ではないのでここにだけ出る。
GPU は <code>docker-compose.image.yml</code> を重ねて立てるか、別マシンのものを
<code>CHIEZO_IMAGE_URL</code> で指す。<strong>モデル(チェックポイント)は自分で置く</strong>
—— 絵と音で別のファイルが要る(音は <code>stable-audio-open</code> か
<code>ace_step</code> を <code>models/checkpoints</code> へ)。</p>
<p class="muted">「答える」層を止めると、ここも全部止まる(MCP の道具も出なくなる)。</p>
</details>
<table class="ai-settings">
<thead><tr><th>相手</th><th>作れるもの</th><th>認証情報</th><th>使う</th><th>課金の形</th></tr></thead>
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
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/ai/enabled")
async def set_enabled(provider: str = Form(...), enabled: str = Form("0")):
    """on/off の切り替え。**on にできない相手は on にしない**（画面側の抑止の裏打ち）。"""
    spec = _require_provider(provider)
    settings_store.require_path()
    want = enabled == "1"
    if want:
        st = settings_store.load(spec.id)
        if spec.credential == providers.CRED_REQUIRED and not st.has_credential:
            raise HTTPException(400, {"error": f"先に「{spec.label}」の認証情報を登録してください"})
        # **「接続を試す」が一度でも通っていないと on にできない。** 到達できるだけでは
        # 話せる保証にならず（認証情報が間違っていても到達はする）、会話して初めて失敗する。
        if not st.verified:
            raise HTTPException(
                400,
                {"error": f"先に「{spec.label}」の「接続を試す」を通してください", "hint": spec.setup},
            )
    settings_store.set_enabled(spec.id, want)
    return RedirectResponse(url="/admin", status_code=303)


def _require_media_owner(provider: str) -> media_providers.MediaProvider:
    """自分の on/off を持つ相手だけを受け付ける。**借り物の相手はここでは触らせない**
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
    # **鍵の要る相手は、鍵が無ければ on にできない。** 有効のまま残しても、
    # 頼むたびに 401 になるだけ(「話す相手」と同じ抑止)
    if want and spec.credential == media_providers.CRED_REQUIRED:
        if not settings_store.load(spec.credential_from or spec.id).credential:
            raise HTTPException(400, {"error": f"先に「{spec.label}」の API キーを登録してください"})

    settings_store.set_enabled(spec.id, want)
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/media/key")
async def set_media_credential(
    provider: str = Form(...), credential: str = Form(""), action: str = Form("")
):
    """**「話す相手」に対応が無い相手**(ElevenLabs)の鍵をここで登録・削除する。

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
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/media/test")
async def test_media_connection(provider: str = Form(...)):
    """絵と音を作る相手と実際に話せるか確かめる(結果はクエリで画面へ返す)。"""
    ok, why = await media.check(provider)
    params = urlencode({"media_tested": provider, "media_ok": "1" if ok else "0", "media_why": why})
    return RedirectResponse(f"/admin?{params}", status_code=303)


@router.post("/admin/ai/layer")
async def set_layer(enabled: str = Form("1")):
    """「答える」層そのものの on/off（元栓）。"""
    settings_store.require_path()
    settings_store.set_answer_enabled(enabled == "1")
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/ai/test")
async def test_connection(provider: str = Form(...)):
    """「接続を試す」。**会話は 1 往復もせず**、相手に軽く聞くだけで確かめる。

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

    # **結果を残す。** これが on を押せるかどうかの根拠になる。
    settings_store.set_verified(spec.id, ok)
    params = {"tested": spec.id, "ok": "1" if ok else "0"}
    if why:
        params["why"] = why[:300]
    return RedirectResponse(url="/admin?" + urlencode(params), status_code=303)


@router.get("/ai/models")
async def list_models(request: Request, backend: str = ""):
    """会話画面がモデルとエフォートのセレクトを組み立てるために引く。

    モデルは相手に聞けたらその一覧、聞けなければ `app/providers.py` の控え。
    エフォートは**聞く口が無い**ので控えだけ（持たない相手では空）。
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
