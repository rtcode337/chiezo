"""動いているイメージの素性(ビルド元のコミットと、焼いた日時)。

タグだけでは、どのコミットが動いているか分からない。 `latest` は上書きされるし、
デプロイ先が pull し忘れていても外からは見えない。画面に出しておけば、手元の
`git log -1` と見比べるだけで反映済みかを確かめられる。

値はビルド時に `--build-arg` で渡して環境変数に焼く。Python には Go の
`runtime/debug.ReadBuildInfo`(VCS 情報が勝手に入る)に当たる仕組みが無いので、
渡さなければ「不明」になる —— **手元ビルドでも渡せるように、Dockerfile 側で
既定値を空にしてある**。
"""
from __future__ import annotations

import os
from datetime import UTC, datetime

# 表示は JST 固定(読む人は日本にいる)。変換と書式は `app/jst.py` に集めてある。
from app import jst

UNKNOWN = "不明(ビルド情報なし)"


def sha() -> str:
    """ビルド元のコミット(完全形)。渡されていなければ空。"""
    return (os.environ.get("CHIEZO_BUILD_SHA") or "").strip()


def built_at() -> datetime | None:
    """イメージを焼いた日時。渡されていない・読めない値なら None。"""
    return _parsed(os.environ.get("CHIEZO_BUILD_TIME") or "")


def describe() -> str:
    """画面に出す 1 行。`2026-08-15 09:12 JST (dbdb1fb)` の形。

    日時を先に置く —— 並べたときに新旧が読めるのは日時のほうで、
    ハッシュは「手元のどのコミットか」を照合するための補助。
    """
    return describe_of(sha(), built_at())


def describe_of(revision: str, when: datetime | str | None) -> str:
    """**他のイメージの素性も同じ書き方で出す**(取り込み・CLI ブリッジ)。

    別々に書式を持たせると、並べたときに同じ意味の行が違う顔で出る ——
    しかも読む人がやりたいのは**見比べる**ことなので、揃っていないと用をなさない。
    日時は文字列(相手が名乗った ISO 8601)でも受ける。
    """
    if isinstance(when, str):
        when = _parsed(when)
    revision = (revision or "").strip()
    if not revision and when is None:
        return UNKNOWN
    short = revision[:7] if revision else "不明"
    if when is None:
        return f"日時不明 ({short})"
    return f"{jst.format(when)} ({short})"


def _parsed(raw: str) -> datetime | None:
    """相手が名乗った日時。読めなければ None(**推測で値を作らない**)。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)
