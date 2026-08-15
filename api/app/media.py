"""生成した画像・音の置き場と、生成の進み具合(ジョブ)の記録。

**なぜ非同期(ジョブ)なのか。** 生成は数秒〜数分かかる。MCP の道具呼び出しで数分待つと
呼び出し側が先に切れるので、頼む口は job を返し、進み具合は別の口で引く。

**なぜ SQLite なのか。** chiezo-api は `--workers 2` で動く。プロセス内の辞書に持つと、
頼んだワーカーと状態を聞かれたワーカーが別だったときに「そんなジョブは無い」になる。
設定 DB(`settings.db`)とは**別ファイル**にする —— あちらは CLI ブリッジが読み取り専用で
マウントしているので、書き込みの多い表を同居させたくない。

**中身は base64 で返さない。** 画像は 1 枚 1〜2MB、音も曲なら同じくらいある。道具の結果は
まるごと呼び出し側のコンテキストに載るので、ファイルに書いて**パスと URL** を返し、
要るときだけ取りに来てもらう。

**絵と音でジョブの表を分けない。** 置き場・掃除・中断の後始末・配信は同じ仕事で、
違うのは頼むときの語彙(サイズ / 長さ)だけ。分けると同じ後始末を 2 つ持つことになる。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import HTTPException

from app import media_backends, media_providers, settings_store

log = logging.getLogger("chiezo.media")

# **走らせたタスクは掴んでおく。** `asyncio.create_task` の戻り値を捨てると、
# イベントループは弱参照しか持たないため**実行中に回収されることがある**
# (生成の途中で黙って止まる)。終わったら外す。
_RUNNING: set[asyncio.Task] = set()

# 置いたものを残す日数。**放っておくと際限なく溜まる**(1 枚 1〜2MB)。
KEEP_DAYS = int(os.environ.get("CHIEZO_MEDIA_KEEP_DAYS", "14") or 14)

# 1 回に頼める枚数の上限。**seed 違いを並べて選ぶ**用途なので少しは要るが、
# GPU を長時間占有させないために止める。
MAX_COUNT = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    backend    TEXT NOT NULL,
    model      TEXT,
    prompt     TEXT NOT NULL,
    size       TEXT,
    seed       INTEGER,
    count      INTEGER NOT NULL DEFAULT 1,
    state      TEXT NOT NULL,
    error      TEXT,
    files      TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sound      TEXT,
    seconds    REAL
);
"""

# 後から足した列。**既にある表は CREATE TABLE IF NOT EXISTS では変わらない**ので、
# 足りないものだけ ALTER する(設定 DB と同じやり方)。
_ADDED_COLUMNS = {"sound": "TEXT", "seconds": "REAL"}


@dataclass
class JobFile:
    path: str
    url: str
    seed: int
    model: str
    # 音の長さ(秒)。**頼んだ秒数ではなく出来たもの**。絵では 0。
    seconds: float = 0.0


def media_dir() -> Path | None:
    """置き場。**未設定なら状態ディレクトリの下**(compose が既にマウントしている)。

    どちらも無い環境では画像生成そのものを無効にする —— 書けない場所へ書きに行って
    実行時に落ちるより、「使えない理由」を先に言うほうがよい。
    """
    raw = os.environ.get("CHIEZO_MEDIA_DIR", "").strip()
    if raw:
        return Path(raw)
    state = settings_store.state_dir()
    return state / "media" if state else None


def is_enabled() -> bool:
    """置き場があるか(作ったものを保存できるか)。"""
    return media_dir() is not None


def tools_enabled() -> bool:
    """MCP に道具を出すか。

    **元栓(「答える」層)が止まっていれば出さない。** 「AI は使わない」と決めた環境で
    絵や音を作る道具だけが並んでいるのは筋が通らないし、押せば 403 になる道具を
    コンテナに載せることになる(使えない道具を並べない、notes と同じ扱い)。
    """
    return is_enabled() and settings_store.answer_enabled()


def require_dir() -> Path:
    path = media_dir()
    if path is None:
        raise HTTPException(
            503,
            {
                "error": "media generation is disabled",
                "hint": "書き込み可能なディレクトリを CHIEZO_MEDIA_DIR("
                "または CHIEZO_STATE_DIR)に設定すると使えるようになる",
            },
        )
    path.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(require_dir() / "jobs.db", timeout=10.0)
    conn.row_factory = sqlite3.Row
    # **WAL にしない。** 置き場はホストのディレクトリをマウントしていることが多く、
    # 共有ファイルシステムでは WAL が使えないことがある(設定 DB と同じ判断)。
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute(_SCHEMA)
    have = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    for name, kind in _ADDED_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
    return conn


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict:
    job = dict(row)
    job["files"] = json.loads(job.get("files") or "[]")
    return job


# 走っているはずの job を「もう動いていない」と見なすまでの猶予。**1 枚ぶんの上限 + 余裕**
# (更新は 1 枚描くごとに入るので、これを超えて無音なら誰も面倒を見ていない)。
STALE_AFTER = media_backends.GENERATE_TIMEOUT + 60


def _reap_stale() -> None:
    """**誰も面倒を見ていない job を畳む。**

    タスクが中断されたときは `_run` が書き残すが、**ワーカーごと落ちた場合は
    そこも通らない**(`--workers 2` で動くので、片方が再起動すれば走っていた生成は消える)。
    running のまま残ると image_status が永遠に running を返し、呼び出し側は待ち続ける。
    """
    limit = (datetime.now(UTC) - timedelta(seconds=STALE_AFTER)).isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE jobs SET state = 'failed', error = ?, updated_at = ?"
            " WHERE state IN ('queued', 'running') AND updated_at < ?",
            ("生成が中断されました(応答が途絶えました)", _now(), limit),
        )


def get_job(job_id: str) -> dict | None:
    _reap_stale()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_dict(row) if row else None


def recent_jobs(limit: int = 20) -> list[dict]:
    _reap_stale()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def _insert(job: dict) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO jobs (id, kind, backend, model, prompt, size, seed, count,"
            " state, error, files, created_at, updated_at, sound, seconds)"
            " VALUES (:id, :kind, :backend, :model, :prompt, :size, :seed, :count,"
            " :state, :error, :files, :created_at, :updated_at, :sound, :seconds)",
            {**job, "files": json.dumps(job["files"], ensure_ascii=False)},
        )


def _update(job_id: str, **fields) -> None:
    if "files" in fields:
        fields["files"] = json.dumps(fields["files"], ensure_ascii=False)
    fields["updated_at"] = _now()
    sets = ", ".join(f"{name} = :{name}" for name in fields)
    with _connect() as conn:
        conn.execute(f"UPDATE jobs SET {sets} WHERE id = :id", {**fields, "id": job_id})


def cleanup(keep_days: int = KEEP_DAYS) -> int:
    """古い画像と記録を消す。消した枚数を返す。"""
    root = media_dir()
    if root is None or not root.exists():
        return 0

    limit = datetime.now(UTC) - timedelta(days=keep_days)
    removed = 0
    for day in root.iterdir():
        if not day.is_dir() or len(day.name) != 8 or not day.name.isdigit():
            continue
        try:
            when = datetime.strptime(day.name, "%Y%m%d").replace(tzinfo=UTC)
        except ValueError:
            continue
        if when >= limit:
            continue
        for file in day.iterdir():
            file.unlink(missing_ok=True)
            removed += 1
        day.rmdir()

    with _connect() as conn:
        conn.execute("DELETE FROM jobs WHERE created_at < ?", (limit.isoformat(),))

    return removed


# MIME → 拡張子。**相手によって形式が違う**(Gemini の絵は JPEG のみ、音は相手ごとに
# mp3 / wav / flac)ので、決め打ちで書くと名前と中身の食い違ったファイルを配ることになる。
_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
    "audio/ogg": "ogg",
}


def _extension(mime: str) -> str:
    """知らない MIME でも**それらしい拡張子**を作る(`audio/aac` → `aac`)。

    落とすところが無いので、最後は種類ごとの無難なものへ寄せる。
    """
    if ext := _EXTENSIONS.get((mime or "").split(";")[0].strip().lower()):
        return ext
    tail = (mime or "").split("/")[-1].split(";")[0].strip().lower()
    if tail.isalnum() and tail:
        return tail
    return "bin"


def _save(job_id: str, index: int, item) -> JobFile:
    """**日付でディレクトリを分ける**(掃除の単位になる)。絵と音で同じ置き方。"""
    day = datetime.now(UTC).strftime("%Y%m%d")
    directory = require_dir() / day
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{job_id}-{index}.{_extension(item.mime)}"
    (directory / name).write_bytes(item.data)

    return JobFile(
        path=str(directory / name),
        # chiezo-api が配る URL(`GET /media/<日付>/<名前>`)
        url=f"/media/{day}/{name}",
        seed=item.seed,
        model=item.model,
        seconds=round(getattr(item, "seconds", 0.0), 2),
    )


async def _run(job_id: str, backend: str, req, count: int) -> None:
    """頼まれたぶんを順に作って記録する。**1 つごとに書く** —— 途中で失敗しても、
    そこまでの成果は残す(GPU の時間を捨てない)。

    絵と音で違うのは呼ぶ関数だけなので、進み方の面倒はここに 1 つだけ置く。
    """
    audio = isinstance(req, media_backends.AudioRequest)
    _update(job_id, state="running")
    files: list[dict] = []
    try:
        for index in range(count):
            # seed は 1 つごとにずらす(同じ頼みで同じものが並んでも選べない)
            one = replace(req, seed=(req.seed + index) if req.seed else 0)
            item = await (
                media_backends.generate_audio(backend, one)
                if audio
                else media_backends.generate(backend, one)
            )
            files.append(asdict(_save(job_id, index, item)))
            _update(job_id, files=files, model=item.model, seed=files[0]["seed"])
        _update(job_id, state="done", files=files)
    except asyncio.CancelledError:
        # **中断は Exception ではない**(BaseException)ので、下の except では拾えない。
        # ここで書き残さないと job は running のまま永久に残る —— 実際に MCP の接続が
        # 切れた拍子にこのタスクごと畳まれ、ComfyUI 側は描き上がっているのに
        # image_status が running を返し続けたことがある。
        log.warning("media job %s cancelled", job_id)
        _update(job_id, state="failed" if not files else "partial",
                error="生成が中断されました", files=files)
        raise
    except Exception as e:
        # **どんな失敗でも記録して返す。** ここで投げても受け取る相手がいない
        # (走っているのは背後のタスク)ので、理由は job に書いて image_status で見せる
        detail = getattr(e, "detail", None)
        message = json.dumps(detail, ensure_ascii=False) if detail else str(e)
        log.warning("media job %s failed: %s", job_id, message[:300])
        # **出来たぶんは残す。** 3 つ頼んで 2 つ出来たなら、その 2 つは使える
        _update(job_id, state="failed" if not files else "partial", error=message[:1000], files=files)


def create_job(
    prompt: str,
    backend: str = "",
    model: str = "",
    size: str = "1024x1024",
    seed: int = 0,
    count: int = 1,
    kind: str = media_providers.KIND_IMAGE,
    sound: str = "",
    seconds: float = 0.0,
) -> dict:
    """頼みを検査して記録するだけ(まだ作らない)。

    **走らせる側と分けてある** —— 実行にはイベントループが要るが、記録は要らない。
    分けておくと、テストは「記録 → 自分で走らせる」の順で確かめられる。

    **無理な頼みはここで断る。** 走らせてから落ちると、呼び出し側は待たされ損になる。
    """
    require_dir()
    if not prompt.strip():
        raise HTTPException(400, {"error": "prompt must not be empty"})
    if not 1 <= count <= MAX_COUNT:
        raise HTTPException(400, {"error": f"count は 1〜{MAX_COUNT} にしてください"})

    chosen = (backend or media_providers.default_backend(kind)).strip().lower()
    spec = media_providers.get(chosen)
    if spec is None or kind not in spec.kinds:
        raise HTTPException(
            404,
            {
                "error": f"unknown backend: {chosen}",
                "backends": [p.id for p in media_providers.all_providers(kind)],
            },
        )

    if kind == media_providers.KIND_AUDIO:
        sound, seconds = _check_audio(spec, sound, seconds)
    else:
        # サイズはここで弾く(走らせてから落ちると待たされ損になる)
        width, height = media_backends.parse_size(size)
        # **描けないサイズも同じ扱い。** 画素をそのまま使う相手は、学習解像度を外れると
        # 崩れた絵を「成功」として返してくる。受け取る側は見るまで気づけないので、
        # GPU を回す前に断る。
        if spec.exact_sizes and f"{width}x{height}" not in spec.sizes:
            raise HTTPException(
                400,
                {
                    "error": f"{spec.label} に {size} は頼めません(モデルの学習解像度から外れ、絵が崩れます)",
                    "sizes": list(spec.sizes),
                    "hint": "小さい素材が要るときは、一覧のサイズで描いてから縮小してください",
                },
            )

    job = {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "backend": chosen,
        "model": model,
        "prompt": prompt.strip(),
        "size": size if kind == media_providers.KIND_IMAGE else None,
        "seed": seed,
        "count": count,
        "state": "queued",
        "error": None,
        "files": [],
        "created_at": _now(),
        "updated_at": _now(),
        "sound": sound or None,
        "seconds": seconds or None,
    }
    _insert(job)
    cleanup()

    return job


def _check_audio(
    spec: media_providers.MediaProvider, sound: str, seconds: float
) -> tuple[str, float]:
    """音の頼みを検査して、記録する値に直す。

    **長さは黙って丸めない。** 上限を超えた頼みを短くして返すと、呼んだ側は
    「頼んだ尺で出来た」と思ったまま短い素材を受け取る —— 断って相手を選び直させる。
    """
    sound = (sound or media_providers.SOUND_SFX).strip().lower()
    if sound not in media_providers.SOUNDS:
        raise HTTPException(
            400,
            {"error": f"sound は {' / '.join(media_providers.SOUNDS)} のどれかにしてください"},
        )

    allowed = media_providers.sounds_of(spec)
    if sound not in allowed:
        raise HTTPException(
            400,
            {
                "error": f"{spec.label} に {sound} は頼めません",
                "sounds": list(allowed),
                "hint": "audio_backends で、その相手に頼める種類を確かめてください",
            },
        )

    limit = media_providers.max_seconds_of(spec, sound)
    if seconds > 0 and limit <= 0:
        # **尺を渡す口が無い相手**(Lyria)。黙って無視すると、頼んだ長さで出来たと
        # 思われる —— 実際にはモデルごとに決まった尺で返る。
        raise HTTPException(
            400,
            {
                "error": f"{spec.label} は長さを指定できません(モデルごとに決まっています)",
                "hint": "長さを決めたいときは自前の GPU か ElevenLabs を選んでください",
            },
        )
    if seconds > limit > 0:
        raise HTTPException(
            400,
            {
                "error": f"{spec.label} に頼める長さは {limit:.0f} 秒までです",
                "hint": "長い曲が要るときは audio_backends で上限の大きい相手を選んでください",
            },
        )

    return sound, media_backends.resolve_seconds(spec, sound, seconds)


def start_image_job(
    prompt: str,
    backend: str = "",
    model: str = "",
    size: str = "1024x1024",
    seed: int = 0,
    count: int = 1,
    negative: str = "",
    steps: int = 25,
) -> dict:
    """頼みを受け付けて job を返す(生成は後ろで走る)。

    **待たない。** 呼び出し側は job を持って帰り、`image_status` で進み具合を見る ——
    生成は数秒〜数分かかり、待たせると呼び出し側が先に切れる。
    """
    job = create_job(prompt, backend=backend, model=model, size=size, seed=seed, count=count)
    request = media_backends.ImageRequest(
        prompt=job["prompt"], negative=negative, size=size, seed=seed, model=model, steps=steps
    )
    return _start(job, request, count)


def start_audio_job(
    prompt: str,
    backend: str = "",
    model: str = "",
    sound: str = media_providers.SOUND_SFX,
    seconds: float = 0.0,
    seed: int = 0,
    count: int = 1,
    lyrics: str = "",
    negative: str = "",
    loop: bool = False,
    steps: int = 50,
) -> dict:
    """音の頼みを受け付けて job を返す(生成は後ろで走る)。絵とまったく同じ扱い。"""
    job = create_job(
        prompt,
        backend=backend,
        model=model,
        seed=seed,
        count=count,
        kind=media_providers.KIND_AUDIO,
        sound=sound,
        seconds=seconds,
    )
    request = media_backends.AudioRequest(
        prompt=job["prompt"],
        sound=job["sound"],
        # **記録した秒数を渡す**(既定に落ちたぶんもここで確定している)
        seconds=job["seconds"] or 0.0,
        lyrics=lyrics,
        negative=negative,
        seed=seed,
        model=model,
        steps=steps,
        loop=loop,
    )
    return _start(job, request, count)


def _start(job: dict, request, count: int) -> dict:
    """後ろで走らせる。**待たない** —— 生成は数秒〜数分かかり、待たせると
    呼び出し側が先に切れる。進み具合は `*_status` で引く。"""
    task = asyncio.create_task(_run(job["id"], job["backend"], request, count))
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)

    return job


async def check(backend: str) -> tuple[bool, str]:
    """その相手と実際に話せるか確かめる(「接続を試す」)。

    **自分の on/off を持つ相手にだけ用意する。** 「話す相手」に対応がある相手は
    あちらに同じ仕組みがあり、鍵も on/off も共通なので、こちらで二重に持たない。
    """
    spec = media_providers.get(backend)
    if spec is None or not spec.owns_toggle:
        raise HTTPException(404, {"error": f"unknown backend: {backend}"})

    if spec.id == "elevenlabs":
        return await _check_elevenlabs(spec)

    try:
        models = await media_backends.comfy_models(media_providers.url_of(spec))
    except Exception as e:  # 立っていない・URL 違い・応答が読めない
        return False, f"繋がりません({type(e).__name__})"

    if not models:
        return False, "繋がりましたが、チェックポイントが 1 つも置かれていません"

    # **絵と音の両方を見る。** 片方しか置いていないのは失敗ではないので、
    # 「繋がった」と言ったうえで**何が作れるか**を返す —— 音のモデルを置き忘れたまま
    # audio_generate を呼んで、初めて気づくのを避ける。
    audio = [name for name in models if media_backends.is_audio_checkpoint(name)]
    picture = [name for name in models if name not in audio]
    parts = [f"絵 {len(picture)} 件", f"音 {len(audio)} 件"]
    if not audio:
        parts.append("(音のチェックポイントは未設置)")
    return True, "、".join(parts) + ": " + "、".join(models[:3])


async def _check_elevenlabs(spec: media_providers.MediaProvider) -> tuple[bool, str]:
    """鍵が通るかを確かめる。**音は作らない** —— 試すたびに枠を食うのは筋が悪い。"""
    key = media_backends.credential_of(spec)
    if not key:
        return False, "API キーが未登録です"
    try:
        # **相手を叩く口は 1 つに寄せる**(テストが差し替えるのもここ)
        async with media_backends._client(10.0) as client:
            res = await client.get(
                f"{media_providers.url_of(spec)}/user", headers={"xi-api-key": key}
            )
    except httpx.HTTPError as e:
        return False, f"繋がりません({type(e).__name__})"

    if res.status_code == 401:
        return False, "API キーが通りませんでした(401)"
    if res.status_code >= 400:
        return False, f"{res.status_code} が返りました"

    tier = ""
    try:
        tier = str(res.json().get("subscription", {}).get("tier") or "")
    except ValueError:
        pass
    return True, f"鍵が通りました({tier})" if tier else "鍵が通りました"


async def backends(kind: str = media_providers.KIND_IMAGE) -> list[dict]:
    """その kind を作れる相手と、選べるモデル。**使えない相手も理由つきで出す** ——
    出さないと「なぜ選べないのか」が分からない。

    **絵と音で一覧を分ける。** 混ぜると、頼めない相手が並んで見えてしまう
    (Lyria に効果音は頼めないし、ElevenLabs に絵は描けない)。
    """
    audio = kind == media_providers.KIND_AUDIO
    out = []
    for spec in media_providers.all_providers(kind):
        models = list(spec.audio_models if audio else spec.models)
        usable, reason = True, ""
        if reason := media_backends.unusable_reason(spec):
            usable = False
        elif spec.id == "comfyui":
            try:
                models = await (
                    media_backends.comfy_audio_models(media_providers.url_of(spec))
                    if audio
                    else media_backends.comfy_models(media_providers.url_of(spec))
                )
            # 立っていない・繋がらない・応答が読めない —— どれも「使えない」で足りる
            except Exception:
                usable, reason = False, "繋がらない(立ち上げていないか URL 違い)"
            else:
                if not models:
                    usable, reason = False, (
                        "音のチェックポイントが置かれていない" if audio
                        else "チェックポイントが置かれていない"
                    )

        entry = {
            "id": spec.id,
            "label": spec.label,
            "usable": usable,
            "reason": reason,
            "models": models,
            "billing": spec.billing,
            "setup": spec.setup,
            "url": media_providers.url_of(spec) if spec.url_env else "",
            # 自分の on/off と「接続を試す」を持つか(画面がボタンを出すかの判断)
            "owns_toggle": spec.owns_toggle,
            "enabled": settings_store.load(spec.id).enabled if spec.owns_toggle else None,
        }
        if audio:
            # **0 は「長さを指定できない」**(モデルで決まる)。呼ぶ側が秒数を
            # 渡すかどうかをここで判断できるようにする。
            entry["sounds"] = {
                sound: media_providers.max_seconds_of(spec, sound)
                for sound in media_providers.sounds_of(spec)
            }
        else:
            entry["sizes"] = list(spec.sizes)
        out.append(entry)
    return out


# 配信できるファイル名の形。**先頭は英数字**(`..` と隠しファイルを弾くため)。
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def resolve(relative: str) -> Path:
    """配信のためにパスを解く。**組み立てる前に形を確かめる**。

    置き場は `<日付 8 桁>/<ファイル名>` の 2 段しかない(`_save` がそう書く)。
    「連結してから外に出ていないか確かめる」書き方でも守れるが、**受け取った文字列が
    パスの組み立てに入ってしまう**ので、読む側にも検査器にも安全だと分からない
    (CodeQL の path injection として上がった)。**先に形で弾いて、通ったものだけ繋ぐ。**
    """
    parts = relative.strip("/").split("/")
    if len(parts) != 2 or not all(_SEGMENT.fullmatch(part) for part in parts):
        raise HTTPException(404, {"error": "not found"})
    day, name = parts
    if len(day) != 8 or not day.isdigit():
        raise HTTPException(404, {"error": "not found"})

    # 形を通ったうえで、**実体が置き場の中にあることも確かめる** —— 中に外を指す
    # シンボリックリンクが混ざっても外へ出さない(書くのは chiezo だけなので念のため)。
    root = require_dir().resolve()
    path = root / day / name
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise HTTPException(404, {"error": "not found"})
    return path
