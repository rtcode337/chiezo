"""イメージに載るファイル。**手元のテストはリポジトリを直接読むので、載せ忘れを拾えない**。

取り込みのイメージがモジュールを名指しでコピーしていた頃、足した `ingest/download.py` が
載らず、osm・geonames・wikipedia のアダプタが import で落ちてカタログごと引けなくなった
(初期化の一覧から osm が消え、再構築は 500 になった)。テストは全部通っていた。
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _copied(dockerfile: Path) -> list[str]:
    """`COPY` の送り元(`--from=` で別のステージから取るものは除く)。"""
    names: list[str] = []
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*COPY\s+(?!--from)(.+)", line)
        if m:
            names += m.group(1).split()[:-1]
    return names


def _is_copied(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def test_every_ingest_module_is_in_the_image():
    patterns = _copied(ROOT / "ingest" / "Dockerfile")
    missing = [p.name for p in (ROOT / "ingest").glob("*.py") if not _is_copied(p.name, patterns)]
    assert missing == [], f"取り込みのイメージに載らないモジュール: {missing}"
    assert _is_copied("sources", patterns)


def test_every_app_module_is_in_the_image():
    patterns = _copied(ROOT / "app" / "Dockerfile")
    missing = [p.name for p in (ROOT / "app").glob("*.py") if not _is_copied(p.name, patterns)]
    assert missing == [], f"app のイメージに載らないモジュール: {missing}"
