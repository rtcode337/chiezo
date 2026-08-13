"""管理画面から入れた設定の置き場（`state/settings.db`）。

ここに入るのは**ユーザーが決めるものだけ** — どの相手を使うか（on/off）、API キー、
既定のモデル。URL や表示名は `app/providers.py` に決め打ちしてあるので入らない。

設計の要点:

- **`CHIEZO_STATE_DIR` が機能フラグを兼ねる**（未設定なら管理画面から相手を増やせない。
  `CHIEZO_LLM_URL` で環境変数から指す従来の経路は、これとは独立に動く）。
  「使う」層・「覚える」層と同じ流儀にしてある。
- **`/data` とは別のディレクトリにする。** `/data` は読み取り専用でマウントする約束
  （取り込み側だけが書く）で、そこへ設定を混ぜると約束が崩れる。`/notes` に相乗りしないのは、
  あちらが「覚える」層の中身で、消してよいものと消してはいけないものが混ざるため。
- **CLI ブリッジのコンテナがこのファイルを読み取り専用でマウントして読む**
  （認証情報をそこから取る）。そのため **WAL は使わない** —— WAL の読み手は -shm への
  書き込みを要求し、read-only のマウントでは最新の書き込みが見えない。
- **API キーは平文で持つ。** Chiezo は認証なし・LAN 内前提のサービスなので、
  暗号化しても鍵の置き場が同じ機械にある以上、守れるものが増えない。
  代わりに**画面には二度と出さない**（登録の有無と日時だけ返す）。
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException

SCHEMA = """
CREATE TABLE IF NOT EXISTS flags (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_settings (
    provider   TEXT PRIMARY KEY,
    enabled    INTEGER NOT NULL DEFAULT 0,
    api_key    TEXT,
    model      TEXT,
    updated_at TEXT NOT NULL
);
"""


@dataclass
class ProviderSetting:
    provider: str
    enabled: bool = False
    api_key: str = ""
    model: str = ""
    updated_at: str = ""

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)


def state_dir() -> Path | None:
    raw = os.environ.get("CHIEZO_STATE_DIR", "").strip()
    return Path(raw) if raw else None


def is_enabled() -> bool:
    return state_dir() is not None


def db_path() -> Path | None:
    d = state_dir()
    return d / "settings.db" if d else None


def require_path() -> Path:
    path = db_path()
    if path is None:
        raise HTTPException(
            503,
            {
                "error": "settings storage is disabled",
                "hint": "書き込み可能なディレクトリを CHIEZO_STATE_DIR に設定すると、"
                        "管理画面から話す相手を追加できるようになる",
            },
        )
    return path


def _connect() -> sqlite3.Connection:
    path = require_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # **WAL にしない。** このファイルは CLI ブリッジのコンテナが
    # 読み取り専用でマウントして読む（そこから認証情報を取る）。WAL では読み手が
    # -shm への書き込みを要求するため、read-only のマウントでは最新の書き込みが
    # 見えなかったり、開けなかったりする（実際にそれで詰まった）。
    # 書き込みは管理画面を押したときだけなので、既定のロールバックジャーナルで足りる。
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# 「答える」層そのものの on/off。相手を 1 つずつ切らなくても、ここで丸ごと止められる。
FLAG_ANSWER_ENABLED = "answer_enabled"


def answer_enabled() -> bool:
    """「答える」層が有効か（既定は有効）。

    **既定を有効にしてあるが、それで勝手に動き出すことは無い** —— 相手のほうが全部
    既定 off なので、何も有効にしていなければ結局どこへも問い合わせない。
    ここは「相手を 1 つずつ切って回らずに、機能ごと止める」ための元栓である。
    """
    if db_path() is None:
        return True
    with _connect() as conn:
        row = conn.execute("SELECT value FROM flags WHERE key = ?", (FLAG_ANSWER_ENABLED,)).fetchone()
    return row is None or row[0] == "1"


def set_answer_enabled(enabled: bool) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO flags (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (FLAG_ANSWER_ENABLED, "1" if enabled else "0", _now()),
        )


def load_all() -> dict[str, ProviderSetting]:
    """全プロバイダの設定。保存先が無い環境では空を返す（例外にしない）。

    管理画面も会話画面も「設定が無い＝どれも無効」で正しく描けるので、
    ここで落とすと保存先を用意していない環境でページごと見られなくなる。
    """
    if db_path() is None:
        return {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT provider, enabled, COALESCE(api_key, ''), COALESCE(model, ''), updated_at"
            "  FROM provider_settings"
        ).fetchall()
    return {
        r[0]: ProviderSetting(provider=r[0], enabled=bool(r[1]), api_key=r[2], model=r[3], updated_at=r[4])
        for r in rows
    }


def load(provider: str) -> ProviderSetting:
    return load_all().get(provider, ProviderSetting(provider=provider))


def _upsert(provider: str, **fields) -> None:
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    updates = ", ".join(f"{k}=excluded.{k}" for k in fields)
    with _connect() as conn:
        conn.execute(
            f"INSERT INTO provider_settings (provider, {cols}, updated_at)"
            f" VALUES (?, {marks}, ?)"
            f" ON CONFLICT(provider) DO UPDATE SET {updates}, updated_at=excluded.updated_at",
            (provider, *fields.values(), _now()),
        )


def set_enabled(provider: str, enabled: bool) -> None:
    _upsert(provider, enabled=1 if enabled else 0)


def set_api_key(provider: str, api_key: str) -> None:
    """API キーを保存する。**入れただけでは有効にならない**（on は別操作）。"""
    _upsert(provider, api_key=api_key)


def clear_api_key(provider: str) -> None:
    """API キーを消し、同時に無効にする。

    鍵の無い相手を有効のまま残すと、会話のたびに失敗するだけになるため。
    """
    _upsert(provider, api_key=None, enabled=0)


def set_model(provider: str, model: str) -> None:
    """既定のモデルを保存する（会話のたびに選び直せるので、これはその初期値）。"""
    _upsert(provider, model=model)
