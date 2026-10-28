"""月ごとの閲覧数の控え(page_id → 月ごとの閲覧数)。

Wikipedia の取り込みは、毎回その時点で最新の 1 か月分の閲覧数ダンプだけを落とす
(全プロジェクト合算で圧縮 5〜6GB)。**1 か月分だけだと、その月の話題に振られる** ——
新作の映画が公開された月は、同じシリーズの旧作の記事まで何倍も読まれ、知名度が
その月の話題で入れ替わった(2003 年公開の作品が、目録でいちばん多い月 18 万になった)。
落とし直すのは重すぎる(12 か月分で 60GB 前後)ので、**取り込むたびにその月の分を控えて、
控えてある月の中央値を出す**。**平均にしない** —— 平均は跳ね上がった 1 か月にそのまま引っ張られる
(普段 月 2 万の記事が新作公開の月だけ 18 万になると、12 か月平均でも 3.3 万になる)。中央値なら、
跳ねた月が少しあってもほとんど動かない。

置き場は取り込みの側だけが書ける `<CHIEZO_DATA_DIR>/pageviews/<ソース名>.db`
(ソースとして読まれる `*.db` の並びとは別の階層)。**控えるのは月 `MIN_VIEWS` 回以上の
記事だけ**(1 日 1 回に満たない裾まで控えると、1 か月が数百 MB になる)。控えていない月は
0 回として数える。**残すのはいまの月から `KEEP_MONTHS` か月以内**(控えてある新しいほうから
数えない —— 離れた月を足したとき、古い月が残り続けないように)。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

KEEP_MONTHS = 12
MIN_VIEWS = 30
BATCH_SIZE = 50_000


class PageviewHistory:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(
            "CREATE TABLE IF NOT EXISTS views ("
            " page_id INTEGER NOT NULL, period TEXT NOT NULL, views INTEGER NOT NULL,"
            " PRIMARY KEY (page_id, period)) WITHOUT ROWID;"
            "CREATE TABLE IF NOT EXISTS periods (period TEXT PRIMARY KEY);"
        )
        self._months: int | None = None

    def periods(self) -> list[str]:
        """控えてある月(YYYY-MM)。古い順。"""
        return [row[0] for row in self._conn.execute("SELECT period FROM periods ORDER BY period")]

    def record(self, period: str, pairs: Iterable[tuple[int, int]]) -> int:
        """1 か月分を控える(同じ月があれば入れ替える)。控えた記事の数を返す。"""
        self._conn.execute("DELETE FROM views WHERE period = ?", (period,))
        batch: list[tuple[int, str, int]] = []
        kept = 0
        for page_id, views in pairs:
            if views is None or views < MIN_VIEWS:
                continue
            batch.append((int(page_id), period, int(views)))
            if len(batch) >= BATCH_SIZE:
                self._conn.executemany("INSERT INTO views VALUES (?, ?, ?)", batch)
                kept += len(batch)
                batch.clear()
        if batch:
            self._conn.executemany("INSERT INTO views VALUES (?, ?, ?)", batch)
            kept += len(batch)
        self._conn.execute("INSERT OR IGNORE INTO periods VALUES (?)", (period,))
        self._conn.commit()
        self._months = None
        return kept

    def keep_within(self, period: str, months: int = KEEP_MONTHS) -> list[str]:
        """`period` から `months` か月より前の月を捨てる(`period` を含めて `months` か月を残す)。捨てた月を返す。"""
        oldest = months_before(period, months - 1)
        old = [p for p in self.periods() if p < oldest]
        for period in old:
            self._conn.execute("DELETE FROM views WHERE period = ?", (period,))
            self._conn.execute("DELETE FROM periods WHERE period = ?", (period,))
        if old:
            self._conn.commit()
            self._months = None
        return old

    def median(self, page_id: int) -> tuple[int, int] | None:
        """(控えてある月の中央値, 何か月分か)。控えていない月は 0 回として数える。控えが無い記事は None。"""
        if self._months is None:
            self._months = len(self.periods())
        if not self._months:
            return None
        views = [
            row[0]
            for row in self._conn.execute("SELECT views FROM views WHERE page_id = ?", (int(page_id),))
        ]
        if not views:
            return None
        values = sorted(views + [0] * (self._months - len(views)))
        middle = len(values) // 2
        if len(values) % 2:
            return values[middle], self._months
        return round((values[middle - 1] + values[middle]) / 2), self._months

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> PageviewHistory:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def months_before(period: str, count: int) -> str:
    """`period`(YYYY-MM)の `count` か月前(YYYY-MM)。"""
    year, month = (int(part) for part in period.split("-"))
    index = year * 12 + (month - 1) - count
    return f"{index // 12:04d}-{index % 12 + 1:02d}"
