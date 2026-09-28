"""生成多尺寸 favicon PNG（纯标准库：zlib + struct，无图像依赖）。

图形与 brand/logo-mark.svg 同源：品牌蓝渐变圆角方 + 三根上升数据柱 +
折线箭头 + 琥珀端点。素材为自绘几何图形，无版权风险。

用法：
    python scripts/gen_favicons.py
输出（写入 static/ 目录，页面以 <link rel="icon"> 引用）：
    favicon-16.png / favicon-32.png / favicon-48.png / apple-touch-icon.png(180)

替换方式：改本文件的 COLORS/几何参数后重跑；或直接用设计工具导出同名 PNG 覆盖。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parents[1] / "src" / "bdp" / "api" / "static"
SIZES = [16, 32, 48, 180]

BLUE_TOP = (24, 144, 255)     # #1890ff 品牌蓝
BLUE_BOTTOM = (9, 109, 217)   # #096dd9
WHITE = (255, 255, 255)
AMBER = (250, 173, 20)        # #faad14


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def _rounded_rect(x: float, y: float, w: float, h: float, r: float) -> bool:
    """点是否在圆角矩形内。"""
    if x < 0 or y < 0 or x >= w or y >= h:
        return False
    cx = min(x, w - x - 1)
    cy = min(y, h - y - 1)
    if cx >= r or cy >= r:
        return True
    dx, dy = r - cx, r - cy
    return dx * dx + dy * dy <= r * r


def render(size: int) -> bytes:
    """按 64 视口等比缩放到 size，逐像素光栅化。"""
    s = size / 64.0
    px = bytearray()
    r_corner = 14 * s
    # 柱形：(x, y, w, h, alpha)
    bars = [
        (14 * s, 38 * s, 8 * s, 14 * s, 0.85),
        (28 * s, 29 * s, 8 * s, 23 * s, 0.92),
        (42 * s, 20 * s, 8 * s, 32 * s, 1.0),
    ]
    # 折线（粗线段，用距离近似）
    line_pts = [(16 * s, 35 * s), (30 * s, 26 * s), (45 * s, 15 * s)]
    line_w = 1.5 * s
    dot_c = (50 * s, 13 * s)
    dot_r = 4.5 * s

    def dist_seg(p, a, b):
        ax, ay = a; bx, by = b; x, y = p
        dx, dy = bx - ax, by - ay
        if dx == dy == 0:
            return ((x - ax) ** 2 + (y - ay) ** 2) ** 0.5
        t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
        return ((x - (ax + t * dx)) ** 2 + (y - (ay + t * dy)) ** 2) ** 0.5

    for y in range(size):
        row = bytearray()
        for x in range(size):
            yy, xx = y + 0.5, x + 0.5
            if not _rounded_rect(xx, yy, size, size, r_corner):
                row += b"\x00\x00\x00\x00"
                continue
            # 背景：对角渐变
            color = _lerp(BLUE_TOP, BLUE_BOTTOM, (xx + yy) / (2 * size))
            # 前景叠加（后画覆盖先画）
            for bx, by, bw, bh, alpha in bars:
                if bx <= xx < bx + bw and by <= yy < by + bh:
                    color = _lerp(color, WHITE, alpha)
            for i in range(len(line_pts) - 1):
                if dist_seg((xx, yy), line_pts[i], line_pts[i + 1]) <= line_w:
                    color = _lerp(color, (230, 247, 255), 1.0)
            if (xx - dot_c[0]) ** 2 + (yy - dot_c[1]) ** 2 <= dot_r * dot_r:
                color = AMBER
            row += bytes((*color, 255))
        px += b"\x00" + row  # filter 0
    return _png(size, bytes(px))


def _png(size: int, raw: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        name = "apple-touch-icon.png" if size == 180 else f"favicon-{size}.png"
        path = OUT_DIR / name
        path.write_bytes(render(size))
        print(f"generated {path.name} ({size}x{size}, {path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
