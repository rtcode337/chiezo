"""管理画面から入れた設定の置き場（`state/settings.db`）。

ここに入るのは**ユーザーが決めるものだけ** — どの相手を使うか（on/off）、認証情報、
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
  書き込みを要求し、read-only のマウントでは `unable to open database file` になる。
  journal_mode は**ファイルに焼き付く属性**なので、`PRAGMA` を書かないだけでは
  既に WAL のファイルが戻らない。接続のたびに `DELETE` を明示している。
- **認証情報は平文で持つ。** Chiezo は認証なし・LAN 内前提のサービスなので、
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
    credential TEXT,
    model      TEXT,
    -- 「接続を試す」が最後に通った日時。**ここが空の相手は on にできない**。
    -- 認証情報を入れ替えたら消す（新しい情報はまだ確かめていないため）。
    verified_at TEXT,
    updated_at TEXT NOT NULL
);
"""


@dataclass
class ProviderSetting:
    provider: str
    enabled: bool = False
    credential: str = ""
    model: str = ""
    verified_at: str = ""
    updated_at: str = ""

    @property
    def has_credential(self) -> bool:
        return bool(self.credential)

    @property
    def verified(self) -> bool:
        """「接続を試す」が一度でも通ったか。**on にできる条件**。"""
        return bool(self.verified_at)


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


def _migrate(conn: sqlite3.Connection) -> None:
    """列名を変えたときの移行。**何度実行しても同じ結果になるように書くこと。**

    `api_key` → `credential`。中身は相手によって OAuth トークンだったり auth.json の
    中身だったりするので、「API キー」という名前が実態と食い違っていた。
    移行を置かないと、既存の DB を新しいコードが読んだ瞬間に
    `no such column: credential` で落ちる（実際に踏んだ）。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(provider_settings)")}
    if "api_key" in cols and "credential" not in cols:
        conn.execute("ALTER TABLE provider_settings RENAME COLUMN api_key TO credential")
        cols.add("credential")
    if "verified_at" not in cols:
        conn.execute("ALTER TABLE provider_settings ADD COLUMN verified_at TEXT")


def _connect() -> sqlite3.Connection:
    path = require_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # **WAL を明示的に外す。** このファイルは CLI ブリッジのコンテナが読み取り専用で
    # マウントして読む（そこから認証情報を取る）。WAL の読み手は -shm への書き込みを
    # 要求するので、read-only のマウントでは `unable to open database file` になる。
    #
    # **`PRAGMA journal_mode` を書かないだけでは足りない** —— journal_mode は
    # **ファイルに焼き付く属性**で、一度 WAL で作られた DB はコードを直しても WAL のまま。
    # ここで毎回 DELETE を指定して、既存のファイルも戻す（指定済みなら何も起きない）。
    # 書き込みは管理画面を押したときだけなので、ロールバックジャーナルで足りる。
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript(SCHEMA)
    _migrate(conn)
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
            "SELECT provider, enabled, COALESCE(credential, ''), COALESCE(model, ''),"
            "       COALESCE(verified_at, ''), updated_at"
            "  FROM provider_settings"
        ).fetchall()
    return {
        r[0]: ProviderSetting(
            provider=r[0], enabled=bool(r[1]), credential=r[2], model=r[3],
            verified_at=r[4], updated_at=r[5],
        )
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


def set_credential(provider: str, credential: str) -> None:
    """認証情報を保存する。**入れただけでは有効にならない**（on は別操作）。

    中身は相手によって違う —— Gemini / OpenRouter は API キー、Claude Code は
    `claude setup-token` の OAuth トークン、Codex は `~/.codex/auth.json` の中身。
    **「API キー」と呼ばないのはそのため**（4 つのうち API キーなのは 2 つだけ）。
    """
    # **確認済みの印を消す。** 入れ替えた認証情報はまだ確かめていないので、
    # 「接続を試す」を通すまで on にできない状態に戻す。
    _upsert(provider, credential=credential, verified_at=None)


def clear_credential(provider: str) -> None:
    """認証情報を消し、同時に無効にする。

    鍵の無い相手を有効のまま残すと、会話のたびに失敗するだけになるため。
    """
    _upsert(provider, credential=None, enabled=0, verified_at=None)


def set_verified(provider: str, ok: bool) -> None:
    """「接続を試す」の結果を記録する。

    **通った相手だけが on にできる。** 登録の有無では、打ち間違えた認証情報も期限切れも
    分からず、会話して初めて失敗する（本番でそれが 502 として出た）。
    失敗したら印を消す —— 一度通ったあとに壊れた相手を、通ったままにしておかない。
    """
    _upsert(provider, verified_at=_now() if ok else None)


def set_model(provider: str, model: str) -> None:
    """既定のモデルを保存する（会話のたびに選び直せるので、これはその初期値）。"""
    _upsert(provider, model=model)
