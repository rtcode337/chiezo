"""ダンプのダウンロード(curl に任せ、進み具合を見張る)。

**落とすのは今までどおり curl**(`-C -` の再開・`--retry` の再試行は curl のほうが
確か)。ここが足すのは見張りだけ —— 書き込み中のファイルの大きさを数秒おきに見て、
**進み具合・速さ・残り時間**をログと `core.report_progress` へ出す。

curl の出力は子プロセスの標準エラーに出るだけで、実行ログにも画面にも届かない。
数 GB のダンプを落としているあいだ、ログは「downloading …」の 1 行で止まったままで、
**進んでいるのか、詰まっているのか、どのくらい遅いのか**が外から読めなかった
(読めないので、待つべきか止めるべきかも決められない)。

**止める印も見る**(`check_stop`)。curl を待っているだけだと、落とし切るまで
「止める」が効かなかった。降りるときは curl を終わらせ、`.part` は残す
(次の回が `-C -` で続きから落とす)。
"""
from __future__ import annotations

import logging
import subprocess
import time
import urllib.request
from pathlib import Path

from core import Stopped, report_progress, stopping

log = logging.getLogger("chiezo.ingest.download")

USER_AGENT = "chiezo-ingest/0.1"
# 画面へ運ぶ値を置き直す間隔(秒)。止める印もこの間隔で見る
POLL_SECONDS = 2.0
# ログへ 1 行書く間隔(秒)。画面の実行ログは末尾の数十行しか持たないので、
# 細かく書くとほかの行を押し流す
LOG_EVERY_SECONDS = 60.0
# 速さは直近のこの秒数で測る(始まりからの平均だと、途中で遅くなっても気づけない)
RATE_WINDOW_SECONDS = 30.0


def _total_size(url: str, user_agent: str) -> int | None:
    """全体の大きさ(Content-Length)。**分からなければ None**(割合と残り時間を出さないだけ)。"""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            length = resp.headers.get("Content-Length")
    except OSError as e:
        log.info("could not read the size of %s: %s", url, e)
        return None
    try:
        return int(length) if length else None
    except ValueError:
        return None


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def progress_line(info: dict) -> str:
    """ログと画面で同じ書き方にする 1 行(`123.4 / 2,345.6 MiB (5.3%) 4.2 MiB/s 残り約 9 分`)。"""
    mib = 1024 * 1024
    done = info["bytes"] / mib
    parts = [f"{info['file']}: {done:,.1f}"]
    if total := info.get("total"):
        parts[0] += f" / {total / mib:,.1f} MiB ({info['bytes'] / total * 100:.1f}%)"
    else:
        parts[0] += " MiB"
    if (rate := info.get("rate")) is not None:
        parts.append(f"{rate / mib:,.2f} MiB/s")
    if (eta := info.get("eta_seconds")) is not None:
        parts.append(f"残り約 {_duration(eta)}")
    return " ".join(parts)


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} 秒"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} 分"
    return f"{seconds // 3600} 時間 {round(seconds % 3600 / 60)} 分"


def _forget_part_from_elsewhere(part: Path, url: str) -> None:
    """途中まで落としたファイルが**別の URL から**のものなら捨てる。どこから落としたかを控える。

    **続きは同じ URL からしかつなげない。** 配布元(ミラー)を切り替えると、ファイル名は
    同じ日付で同じになるのに中身は別物なので、curl の `-C -` が前の途中に別のファイルの
    続きを足し、壊れたファイルが出来上がる(読むまで気づけない)。**控えの無い途中のファイル
    (これを入れる前の版が作ったもの)も捨てる** —— どこから落としたか分からないものに続きを
    つなぐより、落とし直すほうが安い(壊れたファイルは焼くところまで進んでから落ちる)。
    """
    marker = part.with_suffix(part.suffix + ".url")
    if part.exists():
        came_from = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
        if came_from != url:
            log.info("discarding %s (it came from %s, not %s)", part.name, came_from or "an unknown url", url)
            part.unlink()
    part.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(url, encoding="utf-8")


def fetch(url: str, dest: Path, user_agent: str = USER_AGENT) -> Path:
    """`url` を `dest` へ落とす(途中のぶんは `<dest>.part`。既にあれば何もしない)。"""
    part = dest.with_suffix(dest.suffix + ".part")
    if dest.exists() and not part.exists():
        log.info("dump already downloaded: %s", dest)
        return dest
    _forget_part_from_elsewhere(part, url)
    log.info("downloading %s", url)
    total = _total_size(url, user_agent)
    proc = subprocess.Popen(
        ["curl", "-fSL", "-sS", "-A", user_agent, "--retry", "5", "-C", "-", "-o", str(part), url],
    )
    started = time.monotonic()
    first = _size(part)  # 続きから落とすときの起点(速さには入れない)
    samples: list[tuple[float, int]] = [(started, first)]
    last_log = started
    try:
        while True:
            try:
                code = proc.wait(timeout=POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                pass
            if stopping():
                proc.terminate()
                proc.wait()
                raise Stopped("取り込みを止めました(ダウンロードの途中。次の回は続きから落とします)")
            now, size = time.monotonic(), _size(part)
            samples.append((now, size))
            while len(samples) > 2 and now - samples[0][0] > RATE_WINDOW_SECONDS:
                samples.pop(0)
            info = _info(dest.name, size, total, samples)
            report_progress(info)
            if now - last_log >= LOG_EVERY_SECONDS:
                log.info("downloading %s", progress_line(info))
                last_log = now
    finally:
        report_progress(None)
    if code != 0:
        raise subprocess.CalledProcessError(code, ["curl", url])
    elapsed = time.monotonic() - started
    got = _size(part) - first
    log.info(
        "downloaded %s (%.1f MiB in %s, %.2f MiB/s)",
        dest.name, _size(part) / 1024 / 1024, _duration(elapsed),
        got / max(elapsed, 0.001) / 1024 / 1024,
    )
    part.rename(dest)
    part.with_suffix(part.suffix + ".url").unlink(missing_ok=True)
    return dest


def _info(name: str, size: int, total: int | None, samples: list[tuple[float, int]]) -> dict:
    (t0, b0), (t1, b1) = samples[0], samples[-1]
    rate = (b1 - b0) / (t1 - t0) if t1 > t0 else None
    eta = (total - size) / rate if total and rate and rate > 0 and total > size else None
    return {
        "phase": "download", "file": name, "bytes": size, "total": total,
        "rate": rate, "eta_seconds": eta,
    }
