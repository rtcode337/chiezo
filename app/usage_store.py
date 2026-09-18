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

やり取りの目方も残す(`prompt_bytes` / `reply_bytes` / `ms`)。 **中身は持たない**まま
「何をした呼び出しか」を読めるようにするため —— トークン数を言わない相手では、
これが無いと控えに相手の名前と時刻しか残らない。
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
    output_tokens INTEGER,
    -- やり取りの大きさと所要時間。**中身は残さない**(依頼文と応答には呼んだ側の
    -- 材料がそのまま入る)。大きさと時間だけなら中身を持たずに「何をした呼び出しか」
    -- が読める —— 短い問いに長く答えたのか、絵を 1 枚描かせて 6 分待ったのか。
    prompt_bytes  INTEGER,
    reply_bytes   INTEGER,
    ms            INTEGER
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
-- 後から足した列は `_ADDED_QUOTA_COLUMNS` が接続時に足す(`calls` と同じ流儀)。
-- 枠の推移。**上の `quota` は「いまどうか」しか持たない**(聞くたびに上書きする)ので、
-- 「いつ跳ねたか」も「1 ポイントぶんが何回ぶんか」も読めなかった。2 点無いと差が
-- 取れないので、聞いた値をここに積む。
CREATE TABLE IF NOT EXISTS quota_samples (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    provider     TEXT NOT NULL,
    -- 相手は枠を何本も持つ(5 時間と 7 日、モデルのグループごと)。**窓ごとに積む** ——
    -- 混ぜると、別々に減る枠の差が 1 本の線に潰れる。
    window_id    TEXT NOT NULL,
    label        TEXT NOT NULL DEFAULT '',
    at           TEXT NOT NULL,
    used_percent REAL,
    resets_at    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_quota_samples ON quota_samples(provider, window_id, at);
-- 定時に聞きに行く番の取り合い。**`--workers 2` なので両方が同時に起きる** ——
-- 印を 1 つにして、取れたほうだけが聞きに行く(でないと CLI が 2 本立つ)。
CREATE TABLE IF NOT EXISTS quota_polls (
    provider TEXT PRIMARY KEY,
    at       TEXT NOT NULL
);
"""


# 後から足した列。 既にある DB には接続時の ALTER TABLE で足す
# (`ai_log` と同じ流儀。作り直しは要らず、古い行は NULL のまま残る)。
_ADDED_COLUMNS = {
    "prompt_bytes": "INTEGER",
    "reply_bytes": "INTEGER",
    "ms": "INTEGER",
    # 誰が頼んだか（`collect:<名前>` / `api` / …）。無人で回る層のぶんと、
    # 外のアプリが頼んだぶんを後から見分けるため（`app/ai_inflight.py` が持つ印）
    "caller": "TEXT",
    # 考える量。**モデルと同じくらい結果と時間を左右する**のに、失敗の控え
    # （`ai_log`）にしか無かったので、成功した行だけ何で走ったのか読めなかった
    "effort": "TEXT",
    # キャッシュから読んだ入力。**`input_tokens` の内訳**（外に足すものではない）。
    # 同じトークン数でも枠の減り方が違う（API の料金では 1/10 ほど）ので、
    # 分けて持たないと「重い呼び出し」を読み違える
    "cached_tokens": "INTEGER",
    # やり取りの控え（`app/ai_transcript.py`）の id。**同じ 1 回を指す紐**で、
    # これが無いと目方の行と中身の控えを突き合わせられない —— 時刻と相手で
    # 寄せると、同じ秒に並んだ行がずれたときに別の呼び出しの中身を見せる。
    # 控えを止めている・期限で消えた行では空のまま（その回は目方だけが残る）
    "transcript_id": "TEXT",
}

# `quota` に後から足した列。**相手が言ったそのまま** —— 画面に出るのは Chiezo が
# 正規化した名前と割合だけで、元の資料に当たる道が無いと「この行は何か」に
# 答えられない(実測: codex が同じ名前の窓を 2 つ返し、片方が何の制限なのか
# 画面からは分からなかった)。
_ADDED_QUOTA_COLUMNS = {
    "raw": "TEXT NOT NULL DEFAULT ''",
}


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
    for table, added in (("calls", _ADDED_COLUMNS), ("quota", _ADDED_QUOTA_COLUMNS)):
        have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, kind in added.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
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
    effort: str = "",
    kind: str = "chat",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cached_tokens: int | None = None,
    prompt_bytes: int | None = None,
    reply_bytes: int | None = None,
    ms: int | None = None,
    caller: str = "",
    transcript_id: str = "",
) -> None:
    """呼び出しを 1 件残す。失敗しても例外にしない(会話を止めないため)。

    `prompt_bytes` / `reply_bytes` / `ms` は**やり取りの目方**。分からなければ
    `None` のままでよい(古い行と同じ扱いになる)。**中身は渡さない** ——
    ここに残すのは大きさと時間だけで、依頼文も応答も持たない。
    """
    global _last_prune
    if not is_enabled() or not provider:
        return
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO calls (provider, model, effort, kind, at, input_tokens,"
                " output_tokens, cached_tokens, prompt_bytes, reply_bytes, ms, caller,"
                " transcript_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (provider, model or "", effort or "", kind,
                 _now().isoformat(timespec="seconds"),
                 input_tokens, output_tokens, cached_tokens, prompt_bytes, reply_bytes, ms,
                 caller or "", transcript_id or ""),
            )
            now = time.monotonic()
            if now - _last_prune > _PRUNE_INTERVAL:
                _last_prune = now
                cutoff = (_now() - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
                conn.execute("DELETE FROM calls WHERE at < ?", (cutoff,))
                # 枠の推移も同じ日数で捨てる(集計の窓より十分長い)
                conn.execute("DELETE FROM quota_samples WHERE at < ?", (cutoff,))
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


def recent_calls(limit: int = 100) -> list[dict]:
    """成功した呼び出しを新しい順に返す(管理画面の依頼履歴で使う)。

    **集計(`spent`)とは別の口**。あちらは「いくら使ったか」を数えるためのもので、
    こちらは「いつ何を頼んだか」を並べるためのもの —— 同じ表を読むが、
    欲しい形が違う(1 行ずつ・新しい順・件数を絞る)。
    """
    if not is_enabled():
        return []
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT provider, model, effort, kind, at, input_tokens, output_tokens,"
                "       prompt_bytes, reply_bytes, ms, caller, transcript_id FROM calls"
                " ORDER BY at DESC, id DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
    except (sqlite3.Error, OSError) as e:
        log.warning("usage recent_calls failed: %s", e)
        return []
    return [
        {
            "at": r["at"],
            "backend": r["provider"],
            "model": r["model"] or "",
            "effort": r["effort"] or "",
            "kind": r["kind"] or "chat",
            "caller": r["caller"] or "",
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "prompt_bytes": r["prompt_bytes"],
            "reply_bytes": r["reply_bytes"],
            "ms": r["ms"],
            "transcript_id": r["transcript_id"] or "",
        }
        for r in rows
    ]


def breakdown(since: datetime) -> list[dict]:
    """`since` 以降を、相手 × モデル × 考える量 × 依頼元で束ねて返す(多い順)。

    **集計(`spent`)とは束ね方が違うだけの別口**。あちらは相手ごとの合計で
    「どの枠が詰まっているか」を見るためのもの、こちらは詰まった枠の中身を
    「何で走らせた、誰の依頼か」まで割るためのもの —— 相手の合計だけでは、
    枠を食ったのが無人で回る層なのか外のアプリなのかが読めない。

    **目方も返す**(`prompt_bytes` / `reply_bytes`)。CLI を包んだ相手は
    トークン数を言わないので、回数だけでは 20 KB の依頼と 300 KB の依頼が
    同じ 1 回に見える —— 枠を食ったのがどちらかは、そこでしか分からない。

    モデル・考える量・依頼元が空の行は空のまま返す(「無い」と「分からない」を
    呼ぶ側で書き分けられるように、ここでは埋めない)。
    """
    if not is_enabled():
        return []
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT provider, model, effort, caller,"
                "       COUNT(*) AS requests,"
                "       COALESCE(SUM(input_tokens), 0) AS input_tokens,"
                "       COALESCE(SUM(output_tokens), 0) AS output_tokens,"
                "       COALESCE(SUM(cached_tokens), 0) AS cached_tokens,"
                "       SUM(CASE WHEN input_tokens IS NULL AND output_tokens IS NULL"
                "                THEN 1 ELSE 0 END) AS unknown,"
                "       COALESCE(SUM(prompt_bytes), 0) AS prompt_bytes,"
                "       COALESCE(SUM(reply_bytes), 0) AS reply_bytes,"
                "       COALESCE(SUM(ms), 0) AS ms"
                "  FROM calls WHERE at >= ?"
                " GROUP BY provider, model, effort, caller"
                " ORDER BY requests DESC, prompt_bytes DESC",
                (since.astimezone(UTC).isoformat(timespec="seconds"),),
            ).fetchall()
    except (sqlite3.Error, OSError) as e:
        log.warning("usage breakdown failed: %s", e)
        return []
    return [
        {
            "provider": r["provider"],
            "model": r["model"] or "",
            "effort": r["effort"] or "",
            "caller": r["caller"] or "",
            "requests": r["requests"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "cached_tokens": r["cached_tokens"],
            "unknown": r["unknown"] or 0,
            "prompt_bytes": r["prompt_bytes"],
            "reply_bytes": r["reply_bytes"],
            "ms": r["ms"],
        }
        for r in rows
    ]


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


def save_quota(provider: str, windows: list[dict], error: str = "", raw: str = "") -> None:
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
            at = _now().isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO quota (provider, fetched_at, payload, error, raw)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(provider) DO UPDATE SET"
                "   fetched_at=excluded.fetched_at, payload=excluded.payload,"
                "   error=excluded.error, raw=excluded.raw",
                (provider, at, json.dumps(windows, ensure_ascii=False), error, raw),
            )
            # **聞けた値はここにも積む**(`quota` は上書きなので推移が残らない)。
            # 使用率を言わない窓は積まない —— 差を取る相手が無い。
            conn.executemany(
                "INSERT INTO quota_samples"
                " (provider, window_id, label, at, used_percent, resets_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (provider, str(w.get("id") or ""), str(w.get("label") or ""), at,
                     float(w["used_percent"]), str(w.get("resets_at") or ""))
                    for w in windows
                    if isinstance(w, dict) and isinstance(w.get("used_percent"), int | float)
                ],
            )
    except (sqlite3.Error, OSError) as e:
        log.warning("usage quota save failed (%s): %s", provider, e)


def load_quota() -> dict[str, dict]:
    """控えてある枠を相手ごとに。`{provider: {fetched_at, windows, error}}`。"""
    if not is_enabled():
        return {}
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT provider, fetched_at, payload, error, raw FROM quota"
            ).fetchall()
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
            "raw": r["raw"] or "",
        }
    return out


def quota_trail(since: datetime) -> list[dict]:
    """枠の推移を、相手 × 窓ごとにまとめて返す(古い順の点つき)。

    **上がったぶんだけを足す**(`climbed`)。窓は転がって明けるので、下がった差は
    「使ったぶんが戻った」であって「使わなかった」ではない —— 素直に引き算すると、
    明けをまたいだ区間の消費が帳消しになる。

    **点が 1 つしか無い窓も返す**(`climbed` は 0)。「まだ差が取れていない」と
    「動いていない」は別なので、呼ぶ側で書き分けられるように点の数を添える。
    """
    if not is_enabled():
        return []
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT provider, window_id, label, at, used_percent, resets_at"
                "  FROM quota_samples WHERE at >= ? AND used_percent IS NOT NULL"
                # **同じ秒に 2 点入りうる**ので id まで見て並べる(積んだ順に読めないと、
                # 上がり下がりの判定が入れ替わる)
                " ORDER BY provider, window_id, at, id",
                (since.astimezone(UTC).isoformat(timespec="seconds"),),
            ).fetchall()
    except (sqlite3.Error, OSError) as e:
        log.warning("usage quota_trail failed: %s", e)
        return []

    trails: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["provider"], r["window_id"])
        trail = trails.setdefault(key, {
            "provider": r["provider"], "window_id": r["window_id"],
            "label": r["label"] or r["window_id"], "points": [], "climbed": 0.0,
            "resets_at": "", "stale": False,
        })
        percent = float(r["used_percent"])
        if trail["points"] and percent > trail["points"][-1]["used_percent"]:
            trail["climbed"] += percent - trail["points"][-1]["used_percent"]
        trail["points"].append({"at": r["at"], "used_percent": percent})
        # 見出しと明ける時刻は新しいほうを採る(窓が明けると次の時刻に変わる)
        trail["label"] = r["label"] or trail["label"]
        trail["resets_at"] = r["resets_at"] or trail["resets_at"]

    # **相手が返さなくなった窓は下へ回す**(`stale`)。窓の名前が変わったり、枠の
    # 出し方が変わったりすると、その名前の線はそこで伸びなくなる —— 混ざったまま
    # 伸びていた頃の線が「上がったぶん」を大きく持っていると、**直したあとも
    # 壊れた線が一番上に居座る**(実際にそう見えた)。
    newest: dict[str, str] = {}
    for trail in trails.values():
        last = trail["points"][-1]["at"]
        newest[trail["provider"]] = max(newest.get(trail["provider"], ""), last)
    for trail in trails.values():
        trail["stale"] = trail["points"][-1]["at"] < newest[trail["provider"]]
    return sorted(
        trails.values(),
        key=lambda t: (t["stale"], -t["climbed"], t["provider"]),
    )


def calls_since(provider: str, at: str) -> int:
    """その時刻より後に、その相手を何回呼んだか。

    **定時に聞きに行くかの判断に使う。** 何も呼んでいない相手の枠を聞きに行っても
    前と同じ値が返るだけで、CLI を 1 本起こすぶんだけ損をする。

    **その時刻を含めて数える。** 時刻は秒までしか持たないので、点を控えたのと
    同じ秒に入った呼び出しを「より後」で切ると黙って落とす —— 取りこぼすより、
    余分に 1 回聞きに行くほうが安全(`/v1/{source}/recent` の `since` と同じ判断)。
    """
    if not is_enabled() or not provider:
        return 0
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM calls WHERE provider = ? AND at >= ?",
                (provider, at or ""),
            ).fetchone()
    except (sqlite3.Error, OSError) as e:
        log.warning("usage calls_since failed (%s): %s", provider, e)
        return 0
    return int(row["n"]) if row else 0


def last_quota_sample_at(provider: str) -> str:
    """その相手の枠を最後に控えた時刻(無ければ空)。"""
    if not is_enabled() or not provider:
        return ""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT MAX(at) AS at FROM quota_samples WHERE provider = ?", (provider,)
            ).fetchone()
    except (sqlite3.Error, OSError) as e:
        log.warning("usage last_quota_sample_at failed (%s): %s", provider, e)
        return ""
    return str(row["at"] or "") if row else ""


def claim_quota_poll(provider: str, interval: timedelta) -> bool:
    """定時に聞きに行く番を取る。取れたときだけ True。

    **`--workers 2` なので、同じ周期で両方が起きる。** 印を 1 つにして
    「前回より `interval` だけ前より古いときだけ書き換えられる」形にすると、
    書き換えられたほう 1 つだけが聞きに行く —— 数える前に取り合うので、
    2 本の CLI が同時に立つことがない。

    **境界は「より古い」で見る**(`<`)。時刻は秒までしか持たないので、
    同じ秒に起きた 2 つを「以下」で通すと、後から来たほうが書き換わった直後の
    値を見て**両方とも番を取る**(排他の意味が消える)。

    **失敗しても False を返すだけ**(聞きに行かないだけで、会話は止めない)。
    """
    if not is_enabled() or not provider:
        return False
    now = _now()
    cutoff = (now - interval).isoformat(timespec="seconds")
    try:
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO quota_polls (provider, at) VALUES (?, ?)"
                " ON CONFLICT(provider) DO UPDATE SET at = excluded.at"
                " WHERE quota_polls.at < ?",
                (provider, now.isoformat(timespec="seconds"), cutoff),
            )
            return cur.rowcount > 0
    except (sqlite3.Error, OSError) as e:
        log.warning("usage claim_quota_poll failed (%s): %s", provider, e)
        return False


def calls_between(provider: str, start: str, end: str) -> list[dict]:
    """2 つの時刻のあいだに、その相手へ投げた依頼を束ねて返す(多い順)。

    **枠が動いた区間に何が走っていたかを読むためのもの。** 使用率だけでは
    「跳ねた」までしか分からず、止める相手を決められない。

    **始まりは含めず、終わりは含める。** 区間は前の観測の直後から今の観測までで、
    始まりを含めると**隣り合う区間が同じ呼び出しを二度数える**(観測のちょうどその
    秒に入ったぶんが両方に出る)。
    """
    if not is_enabled() or not provider:
        return []
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT caller, model, effort,"
                "       COUNT(*) AS requests,"
                "       COALESCE(SUM(prompt_bytes), 0) AS prompt_bytes,"
                "       COALESCE(SUM(input_tokens), 0) AS input_tokens,"
                "       COALESCE(SUM(output_tokens), 0) AS output_tokens"
                "  FROM calls WHERE provider = ? AND at > ? AND at <= ?"
                " GROUP BY caller, model, effort"
                " ORDER BY requests DESC, prompt_bytes DESC",
                (provider, start or "", end or ""),
            ).fetchall()
    except (sqlite3.Error, OSError) as e:
        log.warning("usage calls_between failed (%s): %s", provider, e)
        return []
    return [
        {
            "caller": r["caller"] or "",
            "model": r["model"] or "",
            "effort": r["effort"] or "",
            "requests": r["requests"],
            "prompt_bytes": r["prompt_bytes"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
        }
        for r in rows
    ]
