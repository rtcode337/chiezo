"""生成した画像・音・動画・音声の置き場と、生成の進み具合(ジョブ)の記録。

なぜ非同期(ジョブ)なのか。 生成は数秒〜数分かかる。MCP の道具呼び出しで数分待つと
呼び出し側が先に切れるので、頼む口は job を返し、進み具合は別の口で引く。

なぜ SQLite なのか。 chiezo-app は `--workers 2` で動く。プロセス内の辞書に持つと、
頼んだワーカーと状態を聞かれたワーカーが別だったときに「そんなジョブは無い」になる。
設定 DB(`settings.db`)とは別ファイルにする —— あちらは CLI ブリッジが読み取り専用で
マウントしているので、書き込みの多い表を同居させたくない。

中身は base64 で返さない。 画像は 1 枚 1〜2MB、音も曲なら同じくらいある。道具の結果は
まるごと呼び出し側のコンテキストに載るので、ファイルに書いてパスと URL を返し、
要るときだけ取りに来てもらう。

kind が違ってもジョブの表を分けない。 置き場・掃除・中断の後始末・配信は同じ仕事で、
違うのは頼むときの語彙(サイズ / 長さ / 尺 / 声)だけ。分けると同じ後始末を kind の数だけ
持つことになる。

文字起こしだけは job にならない。 送る側が既に音を持っていて、返るのは文字(数 KB)
—— 置き場も掃除も配信も要らないので、その場で返すほうが呼ぶ側の手数が少ない。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import os
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import HTTPException

from app import ai_log, media_backends, media_providers, settings_store, usage_store

log = logging.getLogger("chiezo.media")

# 走らせたタスクは掴んでおく。 `asyncio.create_task` の戻り値を捨てると、
# イベントループは弱参照しか持たないため実行中に回収されることがある
# (生成の途中で黙って止まる)。終わったら外す。
_RUNNING: set[asyncio.Task] = set()

# 置いたものを残す日数。放っておくと際限なく溜まる(1 枚 1〜2MB)。
KEEP_DAYS = int(os.environ.get("CHIEZO_MEDIA_KEEP_DAYS", "14") or 14)

# 1 回に頼める枚数の上限。seed 違いを並べて選ぶ用途なので少しは要るが、
# GPU を長時間占有させないために止める。
MAX_COUNT = 4

# 動画だけ別枠。 1 本で数分と数十 MB を使うので、seed 違いを並べて選ぶ用途でも
# 2 本までにしてある(絵と同じ 4 本を許すと、間違えたときの損が大きすぎる)。
MAX_COUNT_VIDEO = 2


def max_count(kind: str) -> int:
    return MAX_COUNT_VIDEO if kind == media_providers.KIND_VIDEO else MAX_COUNT

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
    seconds    REAL,
    voice      TEXT
);
"""

# 後から足した列。既にある表は CREATE TABLE IF NOT EXISTS では変わらないので、
# 足りないものだけ ALTER する(設定 DB と同じやり方)。
# **後から足した列。** 起動時に無ければ ALTER TABLE で足すので、
# 既にある DB でもそのまま動く（作り直しが要らない）。
# group_name … 何案かを 1 組として並べるための名前。頼む側が付ける
# picked_at / picked_note … 画面で「採用」を押した印と、そのときの一言
_ADDED_COLUMNS = {
    "sound": "TEXT", "seconds": "REAL", "voice": "TEXT",
    "group_name": "TEXT", "picked_at": "TEXT", "picked_note": "TEXT",
}


@dataclass
class JobFile:
    path: str
    url: str
    seed: int
    model: str
    # 音の長さ(秒)。頼んだ秒数ではなく出来たもの。絵では 0。
    seconds: float = 0.0


def media_dir() -> Path | None:
    """置き場。未設定なら状態ディレクトリの下(compose が既にマウントしている)。

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

    元栓(「答える」層)が止まっていれば出さない。 「AI は使わない」と決めた環境で
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
    # WAL にしない。 置き場はホストのディレクトリをマウントしていることが多く、
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


# 走っているはずの job を「もう動いていない」と見なすまでの猶予。1 つぶんの上限 + 余裕
# (更新は 1 つ出来るごとに入るので、これを超えて無音なら誰も面倒を見ていない)。
STALE_AFTER = media_backends.GENERATE_TIMEOUT + 60

# 動画は待ち時間の桁が違う。 絵と同じ猶予で畳むと、まだ相手の中で作っている最中の
# job を「中断された」と書いてしまい、出来上がった動画を取りに行けなくなる。
STALE_AFTER_VIDEO = media_backends.VIDEO_TIMEOUT + 60


def _reap_stale() -> None:
    """誰も面倒を見ていない job を畳む。

    タスクが中断されたときは `_run` が書き残すが、**ワーカーごと落ちた場合は
    そこも通らない**(`--workers 2` で動くので、片方が再起動すれば走っていた生成は消える)。
    running のまま残ると image_status が永遠に running を返し、呼び出し側は待ち続ける。

    猶予は kind ごとに変える。 動画だけ桁が違うので、1 つの数字で畳むと
    「まだ作っている最中のものを失敗にする」か「止まったものを何十分も running のまま
    残す」かのどちらかになる。
    """
    now = datetime.now(UTC)
    reason = "生成が中断されました(応答が途絶えました)"
    with _connect() as conn:
        for kind_sql, params, grace in (
            ("kind = ?", [media_providers.KIND_VIDEO], STALE_AFTER_VIDEO),
            ("kind != ?", [media_providers.KIND_VIDEO], STALE_AFTER),
        ):
            limit = (now - timedelta(seconds=grace)).isoformat()
            conn.execute(
                "UPDATE jobs SET state = 'failed', error = ?, updated_at = ?"
                f" WHERE state IN ('queued', 'running') AND {kind_sql} AND updated_at < ?",
                [reason, _now(), *params, limit],
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
            " state, error, files, created_at, updated_at, sound, seconds, voice,"
            " group_name, picked_at, picked_note)"
            " VALUES (:id, :kind, :backend, :model, :prompt, :size, :seed, :count,"
            " :state, :error, :files, :created_at, :updated_at, :sound, :seconds, :voice,"
            " :group_name, :picked_at, :picked_note)",
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


# MIME → 拡張子。相手によって形式が違う(Gemini の絵は JPEG のみ、音は相手ごとに
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
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
}


def _extension(mime: str) -> str:
    """知らない MIME でもそれらしい拡張子を作る(`audio/aac` → `aac`)。

    落とすところが無いので、最後は種類ごとの無難なものへ寄せる。
    """
    if ext := _EXTENSIONS.get((mime or "").split(";")[0].strip().lower()):
        return ext
    tail = (mime or "").split("/")[-1].split(";")[0].strip().lower()
    if tail.isalnum() and tail:
        return tail
    return "bin"


def _save(job_id: str, index: int, item) -> JobFile:
    """日付でディレクトリを分ける(掃除の単位になる)。絵と音で同じ置き方。"""
    day = datetime.now(UTC).strftime("%Y%m%d")
    directory = require_dir() / day
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{job_id}-{index}.{_extension(item.mime)}"
    (directory / name).write_bytes(item.data)

    return JobFile(
        path=str(directory / name),
        # chiezo-app が配る URL(`GET /media/<日付>/<名前>`)
        url=f"/media/{day}/{name}",
        seed=item.seed,
        model=item.model,
        seconds=round(getattr(item, "seconds", 0.0), 2),
    )


async def _run(job_id: str, backend: str, req, count: int, kind: str) -> None:
    """頼まれたぶんを順に作って記録する。1 つごとに書く —— 途中で失敗しても、
    そこまでの成果は残す(GPU の時間を捨てない)。

    絵・音・動画・声で違うのは呼ぶ関数だけなので、進み方の面倒はここに 1 つだけ置く
    (呼び分けは `media_backends.generate_for`)。
    """
    _update(job_id, state="running")
    files: list[dict] = []
    try:
        for index in range(count):
            # seed は 1 つごとにずらす(同じ頼みで同じものが並んでも選べない)
            one = replace(req, seed=(req.seed + index) if req.seed else 0)
            item = await media_backends.generate_for(kind, backend, one)
            # 1 枚 = 1 回。 絵と音も同じサブスクの枠を食う(Codex / Antigravity)ので、
            # 会話と同じ表に残す —— 分けると「話していないのに枠が減った」が読めない。
            usage_store.record(backend, model=item.model, kind=kind)
            files.append(asdict(_save(job_id, index, item)))
            _update(job_id, files=files, model=item.model, seed=files[0]["seed"])
        _update(job_id, state="done", files=files)
    except asyncio.CancelledError:
        # 中断は Exception ではない(BaseException)ので、下の except では拾えない。
        # ここで書き残さないと job は running のまま永久に残る —— 実際に MCP の接続が
        # 切れた拍子にこのタスクごと畳まれ、ComfyUI 側は描き上がっているのに
        # image_status が running を返し続けたことがある。
        log.warning("media job %s cancelled", job_id)
        _update(job_id, state="failed" if not files else "partial",
                error="生成が中断されました", files=files)
        _note_failure(job_id, backend, kind, 0, "生成が中断されました")
        raise
    except Exception as e:
        # どんな失敗でも記録して返す。 ここで投げても受け取る相手がいない
        # (走っているのは背後のタスク)ので、理由は job に書いて image_status で見せる
        detail = getattr(e, "detail", None)
        message = json.dumps(detail, ensure_ascii=False) if detail else str(e)
        log.warning("media job %s failed: %s", job_id, message[:300])
        # 出来たぶんは残す。 3 つ頼んで 2 つ出来たなら、その 2 つは使える
        _update(job_id, state="failed" if not files else "partial", error=message[:1000], files=files)
        _note_failure(job_id, backend, kind, getattr(e, "status_code", 0), message)


def _note_failure(job_id: str, backend: str, kind: str, status: int, reason: str) -> None:
    """生成の失敗も会話と同じ控えに残す(`app/ai_log.py`)。

    **job にも `error` は書いてある**が、あれは頼んだ本人が `image_status` で
    引くためのもので、置き場の掃除(`KEEP_DAYS`)で消える。失敗を後から見に来る人は
    「何の依頼が落ちたか」を job_id で知っているわけではないので、会話の失敗と
    同じ 1 枚の表に並べる。

    **プロンプトの中身は残さない**(大きさだけ)。控えの流儀は会話と揃える。
    """
    job = get_job(job_id) or {}
    ai_log.record(
        backend=backend,
        model=job.get("model") or "",
        effort="",
        status=status,
        reason=reason,
        prompt_bytes=len((job.get("prompt") or "").encode()),
        kind=kind,
    )


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
    voice: str = "",
    group: str = "",
    editing: bool = False,
) -> dict:
    """頼みを検査して記録するだけ(まだ作らない)。

    走らせる側と分けてある —— 実行にはイベントループが要るが、記録は要らない。
    分けておくと、テストは「記録 → 自分で走らせる」の順で確かめられる。

    無理な頼みはここで断る。 走らせてから落ちると、呼び出し側は待たされ損になる。
    """
    require_dir()
    if not prompt.strip():
        raise HTTPException(400, {"error": "prompt must not be empty"})
    limit_count = max_count(kind)
    if not 1 <= count <= limit_count:
        raise HTTPException(400, {"error": f"count は 1〜{limit_count} にしてください"})

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
        if editing:
            _check_reference(spec, sound)
    elif kind == media_providers.KIND_SPEECH:
        # 読み上げに大きさも尺も無い。声だけ確かめる(相手が持っていない声を
        # 渡すと、生成そのものが 400 で返ってくる)
        voice = _check_voice(spec, voice)
    elif kind == media_providers.KIND_VIDEO:
        size, seconds = _check_video(spec, size, seconds, model)
    else:
        # サイズはここで弾く(走らせてから落ちると待たされ損になる)
        width, height = media_backends.parse_size(size)
        # 描けないサイズも同じ扱い。 画素をそのまま使う相手は、学習解像度を外れると
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
        # 直せない相手に元の絵を渡されたら断る。 黙って一から描くと、直したつもりの
        # 絵が全部描き変わって返り、受け取った側は見比べるまで気づけない。
        if editing and not spec.edits:
            raise HTTPException(
                400,
                {
                    "error": f"{spec.label} は元の絵を直せません(一から描くことしかできません)",
                    "backends": [p.id for p in media_providers.all_providers(media_providers.KIND_IMAGE)
                                 if p.edits],
                    "hint": "直せる相手を backend で名指ししてください",
                },
            )

    job = {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "backend": chosen,
        "model": model,
        "prompt": prompt.strip(),
        "size": size if kind in (media_providers.KIND_IMAGE, media_providers.KIND_VIDEO) else None,
        "seed": seed,
        "count": count,
        "state": "queued",
        "error": None,
        "files": [],
        "created_at": _now(),
        "updated_at": _now(),
        "sound": sound or None,
        "seconds": seconds or None,
        "voice": voice or None,
        # **何案かを 1 組として見比べるための名前。** 頼む側が同じ名前を付けると、
        # 画面で横に並ぶ。付けなければ単独の依頼として並ぶ
        "group_name": (group or "").strip() or None,
        "picked_at": None,
        "picked_note": None,
    }
    _insert(job)
    cleanup()

    return job


def _check_reference(spec: media_providers.MediaProvider, sound: str) -> None:
    """参考音源を渡せる頼みかを見る。**黙って無視しない**。

    無視して作ると、出てきた音を「参考にした結果」として受け取ってしまう ——
    似ていない理由が分からないまま、プロンプトのほうを何度も書き直すことになる。
    """
    if not spec.audio_reference:
        raise HTTPException(
            400,
            {
                "error": f"{spec.label} は参考の音を受け取れません",
                "backends": [p.id for p in media_providers.all_providers(media_providers.KIND_AUDIO)
                             if p.audio_reference],
                "hint": "受け取れる相手を backend で名指ししてください",
            },
        )
    if sound != media_providers.SOUND_MUSIC:
        raise HTTPException(
            400,
            {
                "error": "参考の音を渡せるのは曲だけです(効果音の口は文字しか受け取りません)",
                "hint": 'sound="music" で頼んでください',
            },
        )


def _check_audio(
    spec: media_providers.MediaProvider, sound: str, seconds: float
) -> tuple[str, float]:
    """音の頼みを検査して、記録する値に直す。

    長さは黙って丸めない。 上限を超えた頼みを短くして返すと、呼んだ側は
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
        # 尺を渡す口が無い相手(Lyria)。黙って無視すると、頼んだ長さで出来たと
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


def _check_video(
    spec: media_providers.MediaProvider, size: str, seconds: float, model: str
) -> tuple[str, float]:
    """動画の頼みを検査して、記録する値に直す。

    尺は丸めない。 動画の相手は受け付ける値が飛び飛び(Sora は 4/8/12、
    Veo は 4/6/8)なので、近い値へ寄せると「6 秒で頼んだのに 8 秒が返る」になる。
    数分と数十 MB を使ってから気づくのは高いので、頼む前に断る。
    """
    width, height = media_backends.parse_size(size)
    size = f"{width}x{height}"
    if spec.exact_sizes and size not in spec.video_sizes:
        raise HTTPException(
            400,
            {
                "error": f"{spec.label} に {size} の動画は頼めません"
                "(モデルの学習解像度から外れ、絵が崩れます)",
                "sizes": list(spec.video_sizes),
            },
        )

    # omni には尺を渡す口が無い。 黙って無視すると、頼んだ長さで出来たと思われる
    if seconds > 0 and spec.id == "gemini" and not media_backends._is_veo(
        model or spec.video_models[0]
    ):
        raise HTTPException(
            400,
            {
                "error": "Gemini Omni Flash は長さを指定できません(モデルが決めます)",
                "hint": "長さを決めたいときは model に veo-… を指定してください",
            },
        )

    if seconds > 0 and spec.video_seconds and seconds not in spec.video_seconds:
        raise HTTPException(
            400,
            {
                "error": f"{spec.label} に {seconds:.0f} 秒は頼めません",
                "seconds": list(spec.video_seconds),
                "hint": "video_backends で、その相手が受け付ける尺を確かめてください",
            },
        )

    return size, media_backends.resolve_video_seconds(spec, seconds)


def _check_voice(spec: media_providers.MediaProvider, voice: str) -> str:
    """声の名前を検査する。一覧を持っている相手だけ弾く。

    ElevenLabs は登録した声が人によって違うので、こちらに一覧が無い ——
    そういう相手には素通しして、間違っていれば相手のエラーで気づいてもらう
    (こちらで勝手に既定へ倒すと、頼んだ声と違う声で読み上げられる)。
    """
    voice = (voice or "").strip()
    if voice and spec.voices and voice not in spec.voices:
        raise HTTPException(
            400,
            {
                "error": f"{spec.label} に {voice} という声はありません",
                "voices": list(spec.voices),
                "hint": "speech_backends で、その相手の声を確かめてください",
            },
        )
    return voice


def start_image_job(
    prompt: str,
    backend: str = "",
    model: str = "",
    size: str = "1024x1024",
    seed: int = 0,
    count: int = 1,
    negative: str = "",
    steps: int = 25,
    group: str = "",
    source: bytes = b"",
    source_mode: str = "edit",
) -> dict:
    """頼みを受け付けて job を返す(生成は後ろで走る)。

    待たない。 呼び出し側は job を持って帰り、`image_status` で進み具合を見る ——
    生成は数秒〜数分かかり、待たせると呼び出し側が先に切れる。
    """
    job = create_job(prompt, backend=backend, model=model, size=size, seed=seed, count=count,
                     group=group, editing=bool(source))
    request = media_backends.ImageRequest(
        prompt=job["prompt"], negative=negative, size=size, seed=seed, model=model,
        steps=steps, source=source, source_mode=source_mode,
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
    group: str = "",
    source: bytes = b"",
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
        group=group,
        editing=bool(source),
    )
    request = media_backends.AudioRequest(
        prompt=job["prompt"],
        sound=job["sound"],
        # 記録した秒数を渡す(既定に落ちたぶんもここで確定している)
        seconds=job["seconds"] or 0.0,
        lyrics=lyrics,
        negative=negative,
        seed=seed,
        model=model,
        steps=steps,
        loop=loop,
        source=source,
    )
    return _start(job, request, count)


def start_video_job(
    prompt: str,
    backend: str = "",
    model: str = "",
    size: str = "1280x720",
    seconds: float = 0.0,
    seed: int = 0,
    count: int = 1,
    negative: str = "",
    audio: bool = True,
    steps: int = 20,
) -> dict:
    """動画の頼みを受け付けて job を返す。絵や音より待つ(数分〜十数分)。"""
    job = create_job(
        prompt,
        backend=backend,
        model=model,
        size=size,
        seed=seed,
        count=count,
        kind=media_providers.KIND_VIDEO,
        seconds=seconds,
    )
    request = media_backends.VideoRequest(
        prompt=job["prompt"],
        negative=negative,
        # 記録した値を渡す(既定に落ちたぶんもここで確定している)
        size=job["size"] or size,
        seconds=job["seconds"] or 0.0,
        seed=seed,
        model=model,
        steps=steps,
        audio=audio,
    )
    return _start(job, request, count)


def start_speech_job(
    text: str,
    backend: str = "",
    model: str = "",
    voice: str = "",
    speed: float = 1.0,
    language: str = "",
    instructions: str = "",
    seed: int = 0,
    count: int = 1,
) -> dict:
    """読み上げの頼みを受け付けて job を返す。

    `text` を job の prompt として記録する。 絵や音の「こういうものを作って」とは
    性格が違うが、列を分けても後始末・配信・掃除は同じなので、1 つの表に載せている。
    """
    job = create_job(
        text,
        backend=backend,
        model=model,
        seed=seed,
        count=count,
        kind=media_providers.KIND_SPEECH,
        voice=voice,
    )
    request = media_backends.SpeechRequest(
        prompt=job["prompt"],
        voice=job["voice"] or "",
        model=model,
        speed=speed,
        language=language,
        instructions=instructions,
        seed=seed,
    )
    return _start(job, request, count)


# 文字起こしに渡せる音の上限。相手の上限より手前で断る(OpenAI は 25MB)。
# ここは「メモリごと持っていかれない」ための歯止めなので、相手の上限より緩い。
MAX_TRANSCRIBE_BYTES = 200 * 1024 * 1024


async def load_image(path: str = "", url: str = "") -> bytes:
    """直す元の絵を読む。**受け取り方は文字起こしと同じ規則**(置き場の中か URL)。

    ここで受けるのはたいてい `image_status` が返したパスなので、**前に作った絵を
    そのまま直しに出せる**。手元のファイルの絶対パスは受け取らない —— chiezo は
    コンテナの中で動いていて、頼んだ人のディスクは見えない。
    """
    data, _name, _mime = await load_audio(path=path, url=url)
    return data


async def load_audio(path: str = "", url: str = "") -> tuple[bytes, str, str]:
    """文字起こしに渡す音を読む。置き場の中か、サーバーから届く URL からだけ。

    手元のファイルの絶対パスは受け取らない。 chiezo-app はコンテナの中で動くので、
    頼んだ人のディスクは見えない —— 受け取れるように見せると、あるはずのファイルが
    「見つからない」と返ってきて、原因が分からないまま終わる。
    自分のファイルを渡したいときは `POST /v1/media/transcribe` に multipart で送る。
    """
    if path:
        # `/media/<日付>/<名前>` の形だけ。組み立てずに置き場から選ぶ(`resolve`)
        found = resolve(path.split("/media/", 1)[-1])
        data = found.read_bytes()
        name = found.name
    elif url:
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, {"error": "url は http(s) にしてください"})
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            got = await client.get(url)
        if got.status_code >= 400:
            raise HTTPException(502, {"error": f"音を取れませんでした({got.status_code})"})
        data, name = got.content, url.rsplit("/", 1)[-1] or "audio"
    else:
        raise HTTPException(400, {"error": "path か url のどちらかを渡してください"})

    if len(data) > MAX_TRANSCRIBE_BYTES:
        raise HTTPException(
            400,
            {"error": f"音が大きすぎます({len(data) / 1024 / 1024:.0f}MB)",
             "hint": "切り分けてから渡してください"},
        )

    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return data, name, mime


async def transcribe(
    data: bytes,
    filename: str = "audio",
    mime: str = "application/octet-stream",
    backend: str = "",
    model: str = "",
    language: str = "",
) -> dict:
    """音を文字にする。job にしない(返るのが文字なので、その場で返す)。

    置き場も掃除も要らないので `require_dir()` も通さない —— 文字起こしだけは
    「置き場が無い環境」でも使える。
    """
    if not data:
        raise HTTPException(400, {"error": "音のデータが空です"})

    chosen = (backend or media_providers.default_backend(
        media_providers.KIND_TRANSCRIBE)).strip().lower()
    spec = media_providers.get(chosen)
    if spec is None or media_providers.KIND_TRANSCRIBE not in spec.kinds:
        raise HTTPException(
            404,
            {
                "error": f"unknown backend: {chosen}",
                "backends": [
                    p.id for p in media_providers.all_providers(
                        media_providers.KIND_TRANSCRIBE)
                ],
            },
        )

    result = await media_backends.transcribe(
        chosen,
        media_backends.TranscribeRequest(
            data=data, filename=filename, mime=mime, model=model, language=language
        ),
    )
    usage_store.record(chosen, model=result.model, kind=media_providers.KIND_TRANSCRIBE)
    return {"text": result.text, "model": result.model,
            "language": result.language, "backend": chosen}


def _start(job: dict, request, count: int) -> dict:
    """後ろで走らせる。待たない —— 生成は数秒〜数分(動画なら十数分)かかり、
    待たせると呼び出し側が先に切れる。進み具合は `*_status` で引く。

    kind は job から取る。 呼ぶ側にもう一度書かせると、記録と実際に走る処理が
    食い違いうる(記録は動画なのに絵を作る、が起きる)。
    """
    task = asyncio.create_task(
        _run(job["id"], job["backend"], request, count, job["kind"])
    )
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)

    return job


async def check(backend: str) -> tuple[bool, str]:
    """その相手と実際に話せるか確かめる(「接続を試す」)。

    自分の on/off を持つ相手にだけ用意する。 「話す相手」に対応がある相手は
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

    # 絵と音の両方を見る。 片方しか置いていないのは失敗ではないので、
    # 「繋がった」と言ったうえで何が作れるかを返す —— 音のモデルを置き忘れたまま
    # audio_generate を呼んで、初めて気づくのを避ける。
    audio = [name for name in models if media_backends.is_audio_checkpoint(name)]
    picture = [name for name in models if name not in audio]
    parts = [f"絵 {len(picture)} 件", f"音 {len(audio)} 件"]
    if not audio:
        parts.append("(音のチェックポイントは未設置)")
    return True, "、".join(parts) + ": " + "、".join(models[:3])


async def _check_elevenlabs(spec: media_providers.MediaProvider) -> tuple[bool, str]:
    """鍵が通るかを確かめる。音は作らない —— 試すたびに枠を食うのは筋が悪い。"""
    key = media_backends.credential_of(spec)
    if not key:
        return False, "API キーが未登録です"
    try:
        # 相手を叩く口は 1 つに寄せる(テストが差し替えるのもここ)
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
    # 応答が JSON でなくても「鍵は通った」は言える(等級は飾り)
    with contextlib.suppress(ValueError):
        tier = str(res.json().get("subscription", {}).get("tier") or "")
    return True, f"鍵が通りました({tier})" if tier else "鍵が通りました"


# ComfyUI に「何が置いてあるか」を聞く関数と、置いていなかったときの言い方。
# kind ごとに置き場もノードも違う(絵と音は checkpoints、動画は diffusion_models)。
_COMFY_LISTS = {
    media_providers.KIND_IMAGE: (
        media_backends.comfy_image_models, "絵のチェックポイントが置かれていない"),
    media_providers.KIND_AUDIO: (
        media_backends.comfy_audio_models, "音のチェックポイントが置かれていない"),
    media_providers.KIND_VIDEO: (
        media_backends.comfy_video_models,
        "動画のモデルが置かれていない(models/diffusion_models に Wan 系を置く)"),
}


async def backends(kind: str = media_providers.KIND_IMAGE) -> list[dict]:
    """その kind を作れる相手と、選べるモデル。使えない相手も理由つきで出す ——
    出さないと「なぜ選べないのか」が分からない。

    kind ごとに一覧を分ける。 混ぜると、頼めない相手が並んで見えてしまう
    (Lyria に効果音は頼めないし、自前の GPU に読み上げは頼めない)。

    頼むときに要るものが kind ごとに違うので、その kind に効く項目だけを足す
    (絵はサイズ、音は種類と長さ、動画はサイズと尺、声は選べる声)。
    """
    out = []
    for spec in media_providers.all_providers(kind):
        models = list(media_providers.models_of(spec, kind))
        usable, reason = True, ""
        if reason := media_backends.unusable_reason(spec):
            usable = False
        elif spec.id == "comfyui" and kind in _COMFY_LISTS:
            ask, missing = _COMFY_LISTS[kind]
            try:
                models = await ask(media_providers.url_of(spec))
            # 立っていない・繋がらない・応答が読めない —— どれも「使えない」で足りる
            except Exception:
                usable, reason = False, "繋がらない(立ち上げていないか URL 違い)"
            else:
                if not models:
                    usable, reason = False, missing

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
        if kind == media_providers.KIND_AUDIO:
            # 0 は「長さを指定できない」(モデルで決まる)。呼ぶ側が秒数を
            # 渡すかどうかをここで判断できるようにする。
            entry["sounds"] = {
                sound: media_providers.max_seconds_of(spec, sound)
                for sound in media_providers.sounds_of(spec)
            }
        elif kind == media_providers.KIND_VIDEO:
            entry["sizes"] = list(spec.video_sizes)
            # 並んでいる値しか頼めない(音のような「上限」ではない)。
            # 空なら「尺を指定できない相手」。
            entry["seconds"] = list(spec.video_seconds)
        elif kind == media_providers.KIND_SPEECH:
            entry["voices"] = await _voices(spec) if usable else []
        elif kind == media_providers.KIND_IMAGE:
            entry["sizes"] = list(spec.sizes)
        out.append(entry)
    return out


async def _voices(spec: media_providers.MediaProvider) -> list[dict]:
    """選べる声。一覧を持たない相手には聞きに行く —— ElevenLabs は登録した声が
    人によって違うので、こちらで並べると持っていない声を勧めることになる。

    聞けなかったら空を返す(声が引けないだけで相手ごと使えない扱いにはしない ——
    名前を指定すれば読み上げそのものは通る)。
    """
    if spec.voices:
        return [{"id": name, "label": name} for name in spec.voices]
    if spec.id == "elevenlabs":
        with contextlib.suppress(Exception):
            return await media_backends.elevenlabs_voices(spec)
    return []


# 配信できるファイル名の形。先頭は英数字(`..` と隠しファイルを弾くため)。
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _child_named(parent: Path, wanted: str) -> Path | None:
    """`parent` の直下から、その名前の実体を探して返す(組み立てない)。

    `parent / wanted` と書けば 1 行で済むが、それだと**受け取った文字列がパスの
    組み立てに入る**。読む側にも検査器にも安全だと分からず、CodeQL の path injection
    として上がり続ける(実際に上がった)。ここで返す `Path` は置き場を並べて得たもので、
    渡された文字列は照合にしか使っていない。

    置き場は日付ごとに分かれていて 1 日ぶんは高々数百件なので、並べる costs は小さい。
    """
    if not parent.is_dir():
        return None
    return next((entry for entry in parent.iterdir() if entry.name == wanted), None)


def resolve(relative: str) -> Path:
    """配信のためにパスを解く。組み立てる前に形を確かめ、実体は置き場から選ぶ。

    置き場は `<日付 8 桁>/<ファイル名>` の 2 段しかない(`_save` がそう書く)。
    形の検査だけでも `..` は通らないが、組み立てをやめるほうが読む側にも分かりやすい。
    """
    parts = relative.strip("/").split("/")
    if len(parts) != 2 or not all(_SEGMENT.fullmatch(part) for part in parts):
        raise HTTPException(404, {"error": "not found"})
    day, name = parts
    if len(day) != 8 or not day.isdigit():
        raise HTTPException(404, {"error": "not found"})

    root = require_dir().resolve()
    directory = _child_named(root, day)
    path = _child_named(directory, name) if directory else None

    # 実体が置き場の中にあることも確かめる —— 中に外を指すシンボリックリンクが
    # 混ざっても外へ出さない(書くのは chiezo だけなので念のため)。
    if path is None or not path.is_file() or not path.resolve().is_relative_to(root):
        raise HTTPException(404, {"error": "not found"})
    return path


def pick_job(job_id: str, note: str = "") -> dict:
    """その案を「採用」と印す。**依頼した AI はこれを見に来る。**

    人が画面で選んだことを、頼んだ側へ伝えるための唯一の経路。会話で番号を
    伝えてもらう形にすると、AI が別のセッションだったときに拾えない。

    同じ組で先に採用されていたものがあれば、そちらの印は外す —— 1 組から
    選ぶのは 1 つ、という前提で画面も API も作ってある。
    """
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, {"error": f"unknown job: {job_id}"})
    if job.get("group_name"):
        with _connect() as conn:
            conn.execute(
                "UPDATE jobs SET picked_at = NULL, picked_note = NULL"
                " WHERE group_name = ? AND id != ?",
                (job["group_name"], job_id),
            )
    _update(job_id, picked_at=_now(), picked_note=(note or "").strip() or None)
    return get_job(job_id)


def unpick_job(job_id: str) -> dict:
    """採用の印を外す。"""
    if get_job(job_id) is None:
        raise HTTPException(404, {"error": f"unknown job: {job_id}"})
    _update(job_id, picked_at=None, picked_note=None)
    return get_job(job_id)


def picked_jobs(limit: int = 20, group: str = "") -> list[dict]:
    """採用された案を新しい順に。**依頼した AI が引く口。**"""
    _reap_stale()
    where = "picked_at IS NOT NULL"
    params: list = []
    if group:
        where += " AND group_name = ?"
        params.append(group)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE {where} ORDER BY picked_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


# 名前を付けなかった依頼の鍵。 画面の URL に載るので、組の名前と衝突しない形にする。
SINGLE_KEY_PREFIX = "job:"


def group_key(job: dict) -> str:
    """その依頼が属する組の鍵。名前が無ければ依頼そのものを 1 組とする。"""
    return job.get("group_name") or f"{SINGLE_KEY_PREFIX}{job['id']}"


def group_title(job: dict) -> str:
    """一覧に出す見出し。名前が無い依頼は**依頼文の 1 行目**を借りる。

    「(名前なし)」と並べると、どれがどれだか一覧からは選べない —— 見出しは
    中身を開くかどうかを決めるためのものなので、手掛かりを必ず持たせる。
    """
    if name := (job.get("group_name") or "").strip():
        return name
    head = (job.get("prompt") or "").strip().splitlines()
    first = head[0].strip() if head else ""
    return first[:60] or "(依頼文なし)"


def _grouped(rows: list[sqlite3.Row]) -> list[dict]:
    """依頼の行を組にまとめる。**新しい順**、組の中は頼んだ順。"""
    groups: dict[str, dict] = {}
    for row in rows:
        job = _row_to_dict(row)
        key = group_key(job)
        group = groups.setdefault(key, {
            "key": key,
            "group": job.get("group_name") or "",
            "title": group_title(job),
            "kind": job["kind"],
            "created_at": job["created_at"],
            "jobs": [],
        })
        group["jobs"].append(job)
        group["created_at"] = max(group["created_at"], job["created_at"])
    ordered = sorted(groups.values(), key=lambda g: g["created_at"], reverse=True)
    for group in ordered:
        group["jobs"].sort(key=lambda j: j["created_at"])
        # 一覧で使う要約。 中身を開かなくても「何案あって、もう選んだか」が読める
        group["count"] = len(group["jobs"])
        group["picked"] = any(j["picked_at"] for j in group["jobs"])
    return ordered


def job_groups(limit: int = 20) -> list[dict]:
    """見比べる組の一覧。

    名前の無い依頼も 1 件 1 組として出す —— 画面で「まとめ忘れたぶんが消える」と、
    人は生成されなかったと思ってしまう。
    """
    _reap_stale()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit * 8,)
        ).fetchall()
    groups = _grouped(rows)[:limit]
    # 一覧が運ぶのは見出し・日時・種類・件数まで。 案そのものは開いたときに取る
    return [{k: v for k, v in g.items() if k != "jobs"} for g in groups]


def job_group(key: str) -> dict | None:
    """組を 1 つ。**一覧と詳細で口を分ける**ため。

    一覧に全部の案を積んで返すと、開くつもりのない依頼の中身まで毎回運ぶことになる。
    """
    _reap_stale()
    if key.startswith(SINGLE_KEY_PREFIX):
        job = get_job(key[len(SINGLE_KEY_PREFIX):])
        if job is None or job.get("group_name"):
            return None
        with _connect() as conn:
            rows = conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchall()
    else:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE group_name = ? ORDER BY created_at", (key,)
            ).fetchall()
    grouped = _grouped(rows)
    return grouped[0] if grouped else None
