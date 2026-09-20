"""chiezo-app の控え(ログ)の出し方。

**既定では 1 行も出ていなかった。** uvicorn は自分の 3 つのロガーしか設定せず、
root には手を付けない —— `chiezo.*` の実効レベルは WARNING のままで、
**`log.info` は全部捨てられていた**(区画を割った数も、収集が何をしたかも、
1 回の中の段ごとの時間も)。しかも WARNING 以上は Python の最後の受け皿が拾うので、
**時刻もロガー名も付かない裸の行**になる —— `docker logs` を時刻で絞っても
1 件も当たらない(実際にそうなった)。

無人で回る層を抱えている以上、**その場に居合わせない人が後から追えること**が要る。

決めごと:

- **時刻は日本時間**(`app/jst.py` と同じ理由。読むのは画面の前の人で、
  取り込み側の控えも JST で出している)。**固定の +09:00** なので、tzdata の
  入っていないイメージでも壊れない
- **`chiezo` にだけ handler を付ける。** root に付けると uvicorn の行まで
  二重に出る。**`propagate` は切らない** —— 切るとテストの `caplog` が
  拾えなくなる(root には handler が無いので、二重にはならない)
- **何度呼んでも増えない**(import のたびに handler が積み上がらないように)
"""
from __future__ import annotations

import logging
import os
import sys
import time

from app.jst import JST

# この木の下が chiezo のもの(`chiezo.app` / `chiezo.ingest`)
ROOT_NAME = "chiezo"

DEFAULT_LEVEL = "INFO"

_MARK = "_chiezo_handler"


class JstFormatter(logging.Formatter):
    """時刻を日本時間で書く。**固定の +09:00**(夏時間が無いので DB を引かない)。"""

    converter = staticmethod(lambda secs: time.gmtime((secs or 0) + JST.utcoffset(None).seconds))


def setup(level: str | None = None) -> None:
    """`chiezo.*` の控えを標準エラーへ出す。**何度呼んでも増えない**。

    `CHIEZO_LOG_LEVEL` で変えられる(既定 `INFO`)。読めない値は既定に倒す ——
    綴りを間違えただけで控えが丸ごと消えるのは、いちばん困る壊れ方なので。
    """
    logger = logging.getLogger(ROOT_NAME)
    want = (level or os.environ.get("CHIEZO_LOG_LEVEL") or DEFAULT_LEVEL).upper()
    logger.setLevel(getattr(logging, want, None) or logging.INFO)
    if any(getattr(h, _MARK, False) for h in logger.handlers):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JstFormatter("%(asctime)s JST %(levelname)s %(name)s %(message)s"))
    setattr(handler, _MARK, True)
    logger.addHandler(handler)
