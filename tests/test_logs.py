"""chiezo-app の控え(`app/logs.py`)。

**既定では 1 行も出ていなかった。** uvicorn は自分の 3 つのロガーしか設定せず、
`chiezo.*` の実効レベルは WARNING のまま —— 区画を割った数も、収集が何をしたかも、
1 回の中の段ごとの時間も、全部捨てられていた。しかも WARNING 以上は Python の
最後の受け皿が拾うので、**時刻もロガー名も付かない裸の行**になり、
`docker logs` を時刻で絞っても 1 件も当たらなかった。
"""
from __future__ import annotations

import logging
import re

import pytest

from app import logs


@pytest.fixture(autouse=True)
def clean():
    logger = logging.getLogger(logs.ROOT_NAME)
    before = list(logger.handlers), logger.level
    for h in list(logger.handlers):
        logger.removeHandler(h)
    yield
    for h in list(logger.handlers):
        logger.removeHandler(h)
    for h in before[0]:
        logger.addHandler(h)
    logger.setLevel(before[1])


def _emit(capsys, message: str = "区画 8192 個に割った") -> str:
    logs.setup()
    logging.getLogger("chiezo.app").info(message)
    return capsys.readouterr().err


class TestItActuallyPrints:
    def test_info_comes_out(self, capsys):
        assert "区画 8192 個に割った" in _emit(capsys)

    def test_it_says_when_and_who(self, capsys):
        """時刻が無いと `docker logs` を時刻で絞れない(実際に絞れなかった)。"""
        line = _emit(capsys)

        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} JST", line), line
        assert "INFO chiezo.app" in line

    def test_the_clock_is_japan_time(self, capsys):
        """読むのは画面の前の人。取り込み側の控えも JST で出している。"""
        from datetime import datetime

        from app.jst import JST

        line = _emit(capsys)
        stamp = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", line).group(1)
        now = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        assert stamp[:15] == now[:15], f"{stamp} と {now} がずれている"


class TestItDoesNotPileUp:
    def test_calling_it_twice_does_not_double_the_line(self, capsys):
        logs.setup()
        logs.setup()
        logging.getLogger("chiezo.app").info("1 回だけ")

        assert capsys.readouterr().err.count("1 回だけ") == 1

    def test_uvicorn_configuring_afterwards_does_not_silence_it(self, capsys):
        """uvicorn の dictConfig は起動時に走る(こちらは import のとき)。"""
        import logging.config

        from uvicorn.config import LOGGING_CONFIG

        logs.setup()
        logging.config.dictConfig(LOGGING_CONFIG)
        logging.getLogger("chiezo.app").info("あとからでも出る")

        assert "あとからでも出る" in capsys.readouterr().err

    def test_it_keeps_propagating(self, caplog):
        """`propagate` を切ると、テストの `caplog` が拾えなくなる。"""
        logs.setup()
        with caplog.at_level(logging.INFO, logger="chiezo.app"):
            logging.getLogger("chiezo.app").info("拾える")

        assert "拾える" in caplog.text


class TestTheLevel:
    def test_it_can_be_turned_down(self, capsys, monkeypatch):
        monkeypatch.setenv("CHIEZO_LOG_LEVEL", "WARNING")
        logs.setup()
        log = logging.getLogger("chiezo.app")
        log.info("出ない")
        log.warning("出る")

        err = capsys.readouterr().err
        assert "出ない" not in err and "出る" in err

    def test_a_typo_falls_back_instead_of_going_silent(self, capsys, monkeypatch):
        """綴りを間違えただけで控えが丸ごと消えるのは、いちばん困る壊れ方。"""
        monkeypatch.setenv("CHIEZO_LOG_LEVEL", "VERBOSE")

        assert "区画 8192 個に割った" in _emit(capsys)
