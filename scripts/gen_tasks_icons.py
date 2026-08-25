#!/usr/bin/env python3
"""やること画面(PWA)のアイコンを書き出す。標準ライブラリだけで動く。

ホーム画面に追加したときのアイコンは PNG でないと多くのランチャーが受け付けないが、
**この環境には SVG のラスタライザが無い**(rsvg / inkscape / ImageMagick / PIL の
どれも入っていない)。入れると開発機ごとに用意するものが増えるので、
距離関数でアンチエイリアスを掛けて自前で描き、zlib で PNG を組む。

絵柄は `assets/icon.svg`(本を読むロボット)をそのまま起こすのではなく、
**同じ色の別の印**にしてある —— やること画面は知識ベース本体とは別の面で、
ホーム画面に並んだときに見分けが付いたほうがよいため。

    python3 scripts/gen_tasks_icons.py tasks-frontend/public/icons
"""
from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path

# 100 × 100 の座標系で描いて、書き出すときに拡大する。
CORNER_R = 22.0                   # 角丸の半径
CHECK = [(28.0, 52.0), (44.0, 68.0), (74.0, 32.0)]   # チェックの折れ線
STROKE_R = 6.5                    # 線の太さの半分

BG = (0x55, 0x60, 0xE0)           # assets/icon.svg と同じ青
MARK = (0xF2, 0xF4, 0xFF)         # 白に寄せた印の色


def sd_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    pax, pay = px - ax, py - ay
    bax, bay = bx - ax, by - ay
    denom = bax * bax + bay * bay
    h = 0.0 if denom == 0 else max(0.0, min(1.0, (pax * bax + pay * bay) / denom))
    return math.hypot(pax - bax * h, pay - bay * h)


def sd_round_box(px: float, py: float, half: float, radius: float) -> float:
    qx = abs(px) - half + radius
    qy = abs(py) - half + radius
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - radius


def coverage(distance: float, px_per_unit: float) -> float:
    """符号付き距離を 0..1 の被覆率に直す(1 画素ぶんの幅で線形に落とす)。"""
    edge = 0.5 / px_per_unit
    if distance <= -edge:
        return 1.0
    if distance >= edge:
        return 0.0
    return 0.5 - distance / (2.0 * edge)


def render(size: int, rounded: bool) -> list[bytes]:
    """`rounded` が偽なら角を丸めず全面を塗る(maskable 用。OS 側がマスクを掛ける)。"""
    scale = size / 100.0
    rows: list[bytes] = []
    for y in range(size):
        row = bytearray()
        uy = (y + 0.5) / scale
        for x in range(size):
            ux = (x + 0.5) / scale
            bg_a = 1.0 if not rounded else coverage(
                sd_round_box(ux - 50.0, uy - 50.0, 50.0, CORNER_R), scale
            )
            d = min(
                sd_segment(ux, uy, *CHECK[0], *CHECK[1]),
                sd_segment(ux, uy, *CHECK[1], *CHECK[2]),
            ) - STROKE_R
            mark_a = coverage(d, scale)
            r, g, b = (round(BG[i] + (MARK[i] - BG[i]) * mark_a) for i in range(3))
            row += bytes((r, g, b, round(255 * bg_a)))
        rows.append(bytes(row))
    return rows


def png_bytes(size: int, rows: list[bytes]) -> bytes:
    raw = b"".join(b"\x00" + row for row in rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main(out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # maskable だけ角丸なし。こちらで丸めると OS のマスクと二重に掛かって縁が痩せる。
    targets = [
        ("icon-192.png", 192, True),
        ("icon-512.png", 512, True),
        ("icon-512-maskable.png", 512, False),
        ("apple-touch-icon.png", 180, True),
    ]
    for name, size, rounded in targets:
        path = out / name
        path.write_bytes(png_bytes(size, render(size, rounded)))
        print(f"{path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tasks-frontend/public/icons")
