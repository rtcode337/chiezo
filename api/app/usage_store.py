"""使用量の置き場(`state/usage.db`)—— 呼んだ記録と、相手から聞いた枠の控え。

2 つの数を持つ。意味が違うので混ぜない。

- 呼んだ記録(`calls`)…… Chiezo がその相手を何回呼び、何トークン使ったか。
  全部の相手で同じ物差しで測れる代わりに、残りは分からない
  (Chiezo の外で使ったぶん —— 手元の端末で回した Claude Code —— は入らない)。
- 枠の控え(`quota`)…… 相手が言う「使用率と、いつ戻るか」。残りが分かる代わりに、
  聞ける相手が限られる(`app/usage.py` の表)。

`settings.db` に相乗りしない。 あちらは CLI ブリッジが読み取り専用でマウントして
認証情報を読むファイルで、呼ぶたびに書く表を同居させたくない(絵と音のジョブを
別ファイルにしてあるのと同じ判断)。

記録の失敗で会話を止めない。 ここは会話の副産物を残すだけの場所なので、
書けなくても答えは返す(`record()` は例外を投げない)。

トークン数の `NULL` は「相手が言わなかった」、`0` は「使わなかった」。 混ぜると、
数を返さない相手(CLI ブリッジ)が「0 トークンで動く相手」に見える。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app import settings_store

log = logging.getLogger("chiezo.usage")

# 記録を残す日数。放っておくと際限なく溜まる(1 行は小さいが、消す口が無いと
# 何年ぶんも残る)。集計の窓(最長 7 日)より十分長く取ってある。
KEEP_DAYS = int(os.environ.get("CHIEZO_USAGE_KEEP_DAYS", "30") or 30)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    model    TEXT NOT NULL DEFAULT '',
    -- 何のための呼び出しか(chat / image / audio / video / speech / transcribe)。
    -- 分けておかないと「絵を 4 枚頼んだ日だけ回数が跳ねる」理由が読めない。
    kind     TEXT NOT NULL DEFAULT 'chat',
    at       TEXT NOT NULL,
    input_tokens  INTEGER,
    output_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS idx_calls_at ON calls(at);
CREATE TABLE IF NOT EXISTS quota (
    provider   TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    -- 正規化した窓の一覧(JSON)。相手ごとに形が違うので列にはしない ——
    -- 列にすると相手を足すたびに移行が要る。
    payload    TEXT NOT NULL DEFAULT '[]',
    error      TEXT NOT NULL DEFAULT ''
);
"""


@dataclass(frozen=True)
class Spent:
    """ある窓で Chiezo が使ったぶん。"""

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # トークン数を相手が言わなかった呼び出しの数。0 と区別して出す ——
    # 出さないと「回数のわりにトークンが少ない」が測り漏れなのか実態なのか読めない。
    unknown: int = 0

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "unknown_tokens": self.unknown,
        }


def db_path() -> Path | None:
    """置き場。`CHIEZO_STATE_DIR` が無ければ None(記録しない)。"""
    d = settings_store.state_dir()
    return d / "usage.db" if d else None


def is_enabled() -> bool:
    return db_path() is not None


def _connect() -> sqlite3.Connection:
    path = db_path()
    if path is None:  # 呼ぶ側が is_enabled() を見る約束(ここは保険)
        raise RuntimeError("usage store is disabled")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # WAL にしない(設定 DB・ジョブ DB と同じ判断)。置き場はホストのディレクトリを
    # マウントしていることが多く、共有ファイルシステムでは WAL が使えないことがある。
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript(_SCHEMA)
    return conn


def _now() -> datetime:
    return datetime.now(UTC)


# 掃除はこのプロセスで 1 時間に 1 回。書くたびに DELETE を投げても消える行はほとんど
# 無いので、回数のほうを減らす。`--workers 2` で両方が掃除しても結果は同じ(冪等)。
_PRUNE_INTERVAL = 3600.0
_last_prune = 0.0


def record(
    provider: str,
    *,
    model: str = "",
    kind: str = "chat",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """呼び出しを 1 件残す。失敗しても例外にしない(会話を止めないため)。"""
    global _last_prune
    if not is_enabled() or not provider:
        return
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO calls (provider, model, kind, at, input_tokens, output_tokens)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (provider, model or "", kind, _now().isoformat(timespec="seconds"),
                 input_tokens, output_tokens),
            )
            now = time.monotonic()
            if now - _last_prune > _PRUNE_INTERVAL:
                _last_prune = now
                cutoff = (_now() - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
                conn.execute("DELETE FROM calls WHERE at < ?", (cutoff,))
    except (sqlite3.Error, OSError) as e:
        log.warning("usage record failed (%s): %s", provider, e)


def spent(since: datetime) -> dict[str, Spent]:
    """`since` 以降に使ったぶんを相手ごとに。記録が無い相手は入らない。"""
    if not is_enabled():
        return {}
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT provider,"
                "       COUNT(*) AS requests,"
                "       COALESCE(SUM(input_tokens), 0) AS input_tokens,"
                "       COALESCE(SUM(output_tokens), 0) AS output_tokens,"
                "       SUM(CASE WHEN input_tokens IS NULL AND output_tokens IS NULL"
                "                THEN 1 ELSE 0 END) AS unknown"
                "  FROM calls WHERE at >= ? GROUP BY provider",
                (since.astimezone(UTC).isoformat(timespec="seconds"),),
            ).fetchall()
    except (sqlite3.Error, OSError) as e:
        log.warning("usage read failed: %s", e)
        return {}
    return {
        r["provider"]: Spent(
            requests=r["requests"],
            input_tokens=r["input_tokens"],
            output_tokens=r["output_tokens"],
            unknown=r["unknown"] or 0,
        )
        for r in rows
    }


def first_recorded_at() -> str | None:
    """いちばん古い記録の時刻。「いつからの数か」を画面と API に出すため ——
    出さないと、入れたばかりの環境の「0 回」が「使っていない」と読めてしまう。"""
    if not is_enabled():
        return None
    try:
        with _connect() as conn:
            row = conn.execute("SELECT MIN(at) FROM calls").fetchone()
    except (sqlite3.Error, OSError):
        return None
    return row[0] if row and row[0] else None


def save_quota(provider: str, windows: list[dict], error: str = "") -> None:
    """相手から聞いた枠を控える(取れなかったときは理由を控える)。

    取れなかったときに前の値を消さない —— 一時的に繋がらないだけのことがあり、
    直前まで見えていた数字が消えるほうが分かりにくい。画面には「いつ取ったか」と
    「そのあと失敗したこと」を並べて出す。
    """
    if not is_enabled():
        return
    try:
        with _connect() as conn:
            if error and not windows:
                # 取得時刻は動かさない。 一度も取れていない相手に時刻だけ入ると、
                # 「その時刻に何かが取れた」と読める(画面にも API にも出る値なので)。
                conn.execute(
                    "INSERT INTO quota (provider, fetched_at, payload, error) VALUES (?, '', '[]', ?)"
                    " ON CONFLICT(provider) DO UPDATE SET error=excluded.error",
                    (provider, error),
                )
                return
            conn.execute(
                "INSERT INTO quota (provider, fetched_at, payload, error) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(provider) DO UPDATE SET"
                "   fetched_at=excluded.fetched_at, payload=excluded.payload, error=excluded.error",
                (provider, _now().isoformat(timespec="seconds"),
                 json.dumps(windows, ensure_ascii=False), error),
            )
    except (sqlite3.Error, OSError) as e:
        log.warning("usage quota save failed (%s): %s", provider, e)


def load_quota() -> dict[str, dict]:
    """控えてある枠を相手ごとに。`{provider: {fetched_at, windows, error}}`。"""
    if not is_enabled():
        return {}
    try:
        with _connect() as conn:
            rows = conn.execute("SELECT provider, fetched_at, payload, error FROM quota").fetchall()
    except (sqlite3.Error, OSError) as e:
        log.warning("usage quota read failed: %s", e)
        return {}
    out: dict[str, dict] = {}
    for r in rows:
        try:
            windows = json.loads(r["payload"])
        except ValueError:
            windows = []
        out[r["provider"]] = {
            "fetched_at": r["fetched_at"],
            "windows": windows if isinstance(windows, list) else [],
            "error": r["error"] or "",
        }
    return out
