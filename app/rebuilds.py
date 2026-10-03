"""長期記憶(ダンプのソース)の定期再構築。

ダンプは取り込んだ時点の写しなので、放っておくと古くなる —— Wikipedia は月に 2 回、
OSM・GeoNames は日々、J-Quants の上場銘柄一覧は営業日ごとに新しくなる。
これまでは管理画面の「再構築」を人が押すしかなかった。**ソースごとに「何日おき・何時に」を
決めておけば、時計(`main._run_rebuilds`)がその時刻に再構築を起こす**。

決めごと:

- **対象はダンプのソースだけ**(trigger のカタログで `dump` を名乗るもの)。集めたソースは
  収集の巡回が回すので、ここでは扱わない
- **予定の置き場は機械の側**(`app/machine_store.py`。収集の定義と同じ)。1 ソース 1 件の JSON
- **次に走る時刻は「前に起こした日 + 間隔」の、決めた時刻**(日本時間)。起こした時刻ではなく
  **日付で数える** —— 前の回が空き待ちで 3 時間遅れても、次の回は決めた時刻に戻る
- **起こせたときだけ控える**(`mark_started`)。枠が埋まっていて断られた回は控えず、
  次の周でもう一度試す(予定は消えない)
- **1 周に起こすのは 1 本だけ**。ダンプの枠は 1 本ずつなので、並べても次の周に回るだけ
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app import jst, machine_store

log = logging.getLogger("chiezo.rebuilds")

KIND = "rebuild"
# 画面で選べる間隔(日)。自由入力にしない —— 1 日に何度も焼き直す理由が無く、
# 長すぎる間隔は「定期」と呼べない
INTERVAL_CHOICES = (1, 7, 14, 30)
DEFAULT_HOUR = 3  # 日本時間。夜中に焼けば、朝に引く人は新しい世代を読める


@dataclass(frozen=True)
class Schedule:
    source: str
    interval_days: int
    hour: int  # 日本時間 0〜23
    # 前に起こした時刻(UTC の ISO)。まだなら決めた時刻(`set_at`)から数える
    last_started_at: str = ""
    set_at: str = ""

    def next_run_at(self) -> datetime:
        """次に起こす時刻(UTC)。基準の日付 + 間隔の、決めた時刻(日本時間)。

        **一度も起こしていなければ、決めた直後の次の時刻**(今日のその時刻を過ぎていれば明日)。
        決めてから 1 周期待たされると、設定が効いているのか分からない。
        """
        if self.last_started_at:
            base = jst.to_jst(_parse(self.last_started_at))
            day = base.date() + timedelta(days=self.interval_days)
        else:
            base = jst.to_jst(_parse(self.set_at) or datetime.now(UTC))
            day = base.date() if base.hour < self.hour else base.date() + timedelta(days=1)
        at = datetime(day.year, day.month, day.day, self.hour, tzinfo=jst.JST)
        return at.astimezone(UTC)


def _parse(raw: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def is_enabled() -> bool:
    return machine_store.is_enabled()


def get(source: str) -> Schedule | None:
    if not is_enabled():
        return None
    raw = machine_store.get(KIND, source)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return Schedule(
            source=source,
            interval_days=int(data["interval_days"]),
            hour=int(data.get("hour", DEFAULT_HOUR)),
            last_started_at=str(data.get("last_started_at") or ""),
            set_at=str(data.get("set_at") or ""),
        )
    except (ValueError, KeyError, TypeError):
        log.warning("rebuild schedule for %s is unreadable; ignoring", source)
        return None


def all_schedules() -> list[Schedule]:
    if not is_enabled():
        return []
    return [s for key in machine_store.keys(KIND) if (s := get(key)) is not None]


def _put(s: Schedule) -> None:
    machine_store.put(KIND, s.source, json.dumps({
        "interval_days": s.interval_days, "hour": s.hour,
        "last_started_at": s.last_started_at, "set_at": s.set_at,
    }, ensure_ascii=False))


def set_schedule(source: str, interval_days: int, hour: int) -> Schedule:
    """予定を決める(決め直す)。**前に起こした時刻は引き継ぐ** —— 間隔を変えただけで
    すぐ走り出したり、逆に止まったりしないように。"""
    if interval_days not in INTERVAL_CHOICES:
        raise ValueError(f"間隔は {', '.join(map(str, INTERVAL_CHOICES))} 日のどれか")
    if not 0 <= hour <= 23:
        raise ValueError("時刻は 0〜23 時")
    before = get(source)
    s = Schedule(
        source=source, interval_days=interval_days, hour=hour,
        last_started_at=before.last_started_at if before else "",
        set_at=_now_iso(),
    )
    _put(s)
    return s


def clear(source: str) -> None:
    if is_enabled():
        machine_store.drop(KIND, source)


def mark_started(source: str, at: str | None = None) -> None:
    """起こせたことを控える(次の予定はここから数える)。"""
    s = get(source)
    if s is None:
        return
    _put(Schedule(source=s.source, interval_days=s.interval_days, hour=s.hour,
                  last_started_at=at or _now_iso(), set_at=s.set_at))


def due(now: datetime | None = None) -> list[Schedule]:
    """予定の来たもの(遅れているものから)。"""
    now = now or datetime.now(UTC)
    found = [s for s in all_schedules() if s.next_run_at() <= now]
    return sorted(found, key=lambda s: s.next_run_at())
