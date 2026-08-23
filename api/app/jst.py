"""人に見せる日時の書式(日本時間)。

**保存と比較は UTC のまま、表示の直前だけここを通す。** 変換と書式を 1 か所に集めるのは、
画面ごとに書式を書くと同じサーバーの中で表記も時差もばらつくため。

- **`astimezone()` に任せない**(実行環境の TZ 次第で表示が変わる)。api コンテナに
  `TZ` を渡すのはログを読みやすくするためで、画面の正しさをそこに依存させない。
- 時差は `TimeZoneInfo` ではなく**固定の +09:00**。JST に夏時間は無いので足り、
  tzdata の無いコンテナでも壊れない。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

JST = timezone(timedelta(hours=9), "JST")


def to_jst(when: datetime) -> datetime:
    """時差の無い値は UTC とみなして JST へ寄せる。

    DB に入れている ISO 文字列は UTC で書いているが、古い行や外から来た値が
    naive なことがある。ここで UTC を当てておかないと `astimezone()` が
    実行環境の TZ を当ててしまう。
    """
    return (when if when.tzinfo else when.replace(tzinfo=UTC)).astimezone(JST)


def format(when: datetime) -> str:
    """`2026-08-15 09:12 JST`。"""
    return f"{to_jst(when):%Y-%m-%d %H:%M} JST"


def parse(raw: str) -> datetime | None:
    """DB や相手の応答から来た ISO 文字列を読む。読めなければ None。

    **読めない値で画面を落とさない** —— 日時は添え物で、本体(使用量)は出せるため。
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)
