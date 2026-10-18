"""日本の祝日アダプタ(内閣府「国民の祝日」の CSV)。

内閣府が公開している `syukujitsu.csv`(1955 年から翌年までの祝日・休日)を取り込む。
1 日 = 1 文書。カレンダーを出すアプリが、祝日を自前で外へ取りに行かずに済むように持つ
(祝日法の決まりから計算するより、公表そのものを正とする —— 取り違えると予定を誤るため)。

- 見出しは `YYYY-MM-DD 名前`(日付が先なので並べるとそのまま暦順になり、同じ日が 2 件になることもない)
- タグは `祝日` と、年(`2026`)・年月(`2026-09`)。**全件は `filter?tag=祝日` で引く**
  (`filter` は条件を 1 つ要求する)。年や月だけ欲しいときはそのタグで引く
- 脇書き(`extra`)に `date`(YYYY-MM-DD)と `name` を持つ。読む側はこちらを使う
- 振替休日・国民の休日は、内閣府の表のとおり「休日」と書かれている

**公表は翌年の分まで**(例年 2 月ごろに翌年分が載る)。それより先の年は入っていないので、
読む側は「どの年まで載っているか」を最後の日付で判断する。月に 1 回ほど再構築すれば足りる
(管理画面の定期再構築で 30 日おきにしておく)。

CSV は Shift_JIS(CP932)で、1 行目は見出し。データは「政府標準利用規約(第 2.0 版)」で
公開されており、使うときは出典(内閣府「国民の祝日について」)を示す。
"""
from __future__ import annotations

import logging
import os
import urllib.request
from collections.abc import Iterator
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from download import USER_AGENT
from download import fetch as _download

from core import Doc

log = logging.getLogger("chiezo.ingest")

CSV_URL = "https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv"
ALL_TAG = "祝日"


class HolidaysAdapter:
    """内閣府の祝日一覧。"""

    source = "holidays_jp"
    source_kind = "holidays"
    lang = "ja"
    # 1955 年からの全件で 1,000 件を超える。大きく欠けた CSV を焼かないための下限
    min_docs = 500
    min_build_memory_gb = 0.5

    def __init__(self) -> None:
        # 昔の祝日は消えないので、固定の 1 件で検索を確かめてよい
        self.sample_titles: list[str] = ["1955-01-01 元日"]

    def _remote_date(self) -> str:
        """Last-Modified から世代の日付を得る(CSV は日付入りの置き場を持たないため)。"""
        if d := os.environ.get("DUMP_DATE"):
            return d
        req = urllib.request.Request(CSV_URL, method="HEAD", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                last_modified = resp.headers.get("Last-Modified")
            if last_modified:
                return parsedate_to_datetime(last_modified).strftime("%Y%m%d")
        except OSError:
            log.warning("holidays_jp: Last-Modified を読めなかったので今日の日付にする")
        return datetime.now(UTC).strftime("%Y%m%d")

    def fetch(self, workdir: Path) -> tuple[Path, str]:
        workdir.mkdir(parents=True, exist_ok=True)
        stamp = self._remote_date()
        return _download(CSV_URL, workdir / f"{self.source}-{stamp}.csv"), stamp

    def iter_docs(self, path: Path) -> Iterator[Doc]:
        text = path.read_bytes().decode("cp932")
        seen: set[str] = set()
        for line in text.splitlines()[1:]:
            parsed = parse_line(line)
            if parsed is None:
                continue
            day, name = parsed
            iso = day.isoformat()
            if iso in seen:
                continue
            seen.add(iso)
            yield Doc(
                # 日付から番号を作る(同じ日は世代をまたいでも同じ番号になる)
                doc_id=int(day.strftime("%Y%m%d")),
                title=f"{iso} {name}",
                opening=f"{day.year}年{day.month}月{day.day}日は{name}",
                tags=[ALL_TAG, str(day.year), iso[:7]],
                aliases=[iso],
                extra={"date": iso, "name": name},
            )


def parse_line(line: str) -> tuple[date, str] | None:
    """`2026/9/21,敬老の日` を (日付, 名前) にする。読めない行は None。"""
    cells = line.strip().split(",", 1)
    if len(cells) != 2 or not cells[1].strip():
        return None
    try:
        y, m, d = (int(x) for x in cells[0].strip().split("/"))
        return date(y, m, d), cells[1].strip()
    except ValueError:
        return None
