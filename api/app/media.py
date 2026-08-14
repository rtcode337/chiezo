"""生成した画像の置き場と、生成の進み具合(ジョブ)の記録。

**なぜ非同期(ジョブ)なのか。** 生成は数秒〜数分かかる。MCP の道具呼び出しで数分待つと
呼び出し側が先に切れるので、頼む口は job を返し、進み具合は別の口で引く。

**なぜ SQLite なのか。** chiezo-api は `--workers 2` で動く。プロセス内の辞書に持つと、
頼んだワーカーと状態を聞かれたワーカーが別だったときに「そんなジョブは無い」になる。
設定 DB(`settings.db`)とは**別ファイル**にする —— あちらは CLI ブリッジが読み取り専用で
マウントしているので、書き込みの多い表を同居させたくない。

**画像は base64 で返さない。** 1 枚 1〜2MB あり、道具の結果はまるごと呼び出し側の
コンテキストに載る。ファイルに書いて**パスと URL** を返し、要るときだけ取りに来てもらう。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
    updated_at TEXT NOT NULL
);
"""


@dataclass
class JobFile:
    path: str
    url: str
    seed: int
    model: str


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
    """置き場があるか(画像を保存できるか)。"""
    return media_dir() is not None


def tools_enabled() -> bool:
    """MCP に道具を出すか。

    **元栓(「答える」層)が止まっていれば出さない。** 「AI は使わない」と決めた環境で
    絵を描く道具だけが並んでいるのは筋が通らないし、押せば 403 になる道具を
    コンテナに載せることになる(使えない道具を並べない、notes と同じ扱い)。
    """
    return is_enabled() and settings_store.answer_enabled()


def require_dir() -> Path:
    path = media_dir()
    if path is None:
        raise HTTPException(
            503,
            {
                "error": "image generation is disabled",
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
            " state, error, files, created_at, updated_at)"
            " VALUES (:id, :kind, :backend, :model, :prompt, :size, :seed, :count,"
            " :state, :error, :files, :created_at, :updated_at)",
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


def _save(job_id: str, index: int, image: media_backends.GeneratedImage) -> JobFile:
    """**日付でディレクトリを分ける**(掃除の単位になる)。"""
    day = datetime.now(UTC).strftime("%Y%m%d")
    directory = require_dir() / day
    directory.mkdir(parents=True, exist_ok=True)
    # **拡張子は中身に合わせる。** 相手によって形式が違う(Gemini は JPEG しか返さない)ので、
    # png 決め打ちで書くと、名前と中身の食い違ったファイルを配ることになる。
    name = f"{job_id}-{index}.{'jpg' if image.mime == 'image/jpeg' else 'png'}"
    (directory / name).write_bytes(image.data)

    return JobFile(
        path=str(directory / name),
        # chiezo-api が配る URL(`GET /media/<日付>/<名前>`)
        url=f"/media/{day}/{name}",
        seed=image.seed,
        model=image.model,
    )


async def _run(job_id: str, backend: str, req: media_backends.ImageRequest, count: int) -> None:
    """頼まれたぶんを順に描いて記録する。**1 枚ごとに書く** —— 途中で失敗しても、
    そこまでの絵は残す(GPU の時間を捨てない)。"""
    _update(job_id, state="running")
    files: list[dict] = []
    try:
        for index in range(count):
            # seed は 1 枚ごとにずらす(同じ頼みで同じ絵が並んでも選べない)
            one = media_backends.ImageRequest(
                prompt=req.prompt,
                negative=req.negative,
                size=req.size,
                seed=(req.seed + index) if req.seed else 0,
                model=req.model,
                steps=req.steps,
            )
            image = await media_backends.generate(backend, one)
            files.append(asdict(_save(job_id, index, image)))
            _update(job_id, files=files, model=image.model, seed=files[0]["seed"])
        _update(job_id, state="done", files=files)
    except asyncio.CancelledError:
        # **中断は Exception ではない**(BaseException)ので、下の except では拾えない。
        # ここで書き残さないと job は running のまま永久に残る —— 実際に MCP の接続が
        # 切れた拍子にこのタスクごと畳まれ、ComfyUI 側は描き上がっているのに
        # image_status が running を返し続けたことがある。
        log.warning("image job %s cancelled", job_id)
        _update(job_id, state="failed" if not files else "partial",
                error="生成が中断されました", files=files)
        raise
    except Exception as e:
        # **どんな失敗でも記録して返す。** ここで投げても受け取る相手がいない
        # (走っているのは背後のタスク)ので、理由は job に書いて image_status で見せる
        detail = getattr(e, "detail", None)
        message = json.dumps(detail, ensure_ascii=False) if detail else str(e)
        log.warning("image job %s failed: %s", job_id, message[:300])
        # **描けたぶんは残す。** 3 枚頼んで 2 枚描けたなら、その 2 枚は使える
        _update(job_id, state="failed" if not files else "partial", error=message[:1000], files=files)


def create_job(
    prompt: str,
    backend: str = "",
    model: str = "",
    size: str = "1024x1024",
    seed: int = 0,
    count: int = 1,
) -> dict:
    """頼みを検査して記録するだけ(まだ描かない)。

    **走らせる側と分けてある** —— 実行にはイベントループが要るが、記録は要らない。
    分けておくと、テストは「記録 → 自分で走らせる」の順で確かめられる。
    """
    require_dir()
    if not prompt.strip():
        raise HTTPException(400, {"error": "prompt must not be empty"})
    if not 1 <= count <= MAX_COUNT:
        raise HTTPException(400, {"error": f"count は 1〜{MAX_COUNT} にしてください"})

    chosen = (backend or media_providers.default_backend()).strip().lower()
    spec = media_providers.get(chosen)
    if spec is None:
        raise HTTPException(
            404,
            {
                "error": f"unknown backend: {chosen}",
                "backends": [p.id for p in media_providers.all_providers()],
            },
        )
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
        "kind": "image",
        "backend": chosen,
        "model": model,
        "prompt": prompt.strip(),
        "size": size,
        "seed": seed,
        "count": count,
        "state": "queued",
        "error": None,
        "files": [],
        "created_at": _now(),
        "updated_at": _now(),
    }
    _insert(job)
    cleanup()

    return job


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
    task = asyncio.create_task(_run(job["id"], job["backend"], request, count))
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)

    return job


async def check(backend: str) -> tuple[bool, str]:
    """その相手と実際に話せるか確かめる(「接続を試す」)。

    **自前の GPU にだけ用意する。** 外部サービスは「話す相手」側に同じ仕組みがあり、
    鍵も on/off も共通なので、こちらで二重に持たない。
    """
    spec = media_providers.get(backend)
    if spec is None or not spec.owns_toggle:
        raise HTTPException(404, {"error": f"unknown backend: {backend}"})

    try:
        models = await media_backends.comfy_models(media_providers.url_of(spec))
    except Exception as e:  # 立っていない・URL 違い・応答が読めない
        return False, f"繋がりません({type(e).__name__})"

    if not models:
        return False, "繋がりましたが、チェックポイントが 1 つも置かれていません"
    return True, "、".join(models[:3])


async def backends() -> list[dict]:
    """使える相手と、その相手で選べるモデル。**使えない相手も理由つきで出す** ——
    出さないと「なぜ選べないのか」が分からない。"""
    out = []
    for spec in media_providers.all_providers():
        usable, reason, models = True, "", list(spec.models)
        if reason := media_backends.unusable_reason(spec):
            usable = False
        elif spec.id == "comfyui":
            try:
                models = await media_backends.comfy_models(media_providers.url_of(spec))
            # 立っていない・繋がらない・応答が読めない —— どれも「使えない」で足りる
            except Exception:
                usable, reason = False, "繋がらない(立ち上げていないか URL 違い)"
            else:
                if not models:
                    usable, reason = False, "チェックポイントが置かれていない"

        out.append(
            {
                "id": spec.id,
                "label": spec.label,
                "usable": usable,
                "reason": reason,
                "models": models,
                "sizes": list(spec.sizes),
                "billing": spec.billing,
                "setup": spec.setup,
                "url": media_providers.url_of(spec) if spec.url_env else "",
                # 自分の on/off と「接続を試す」を持つか(画面がボタンを出すかの判断)
                "owns_toggle": spec.owns_toggle,
                "enabled": settings_store.load(spec.id).enabled if spec.owns_toggle else None,
            }
        )
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
