"""二维码渲染：终端 / PNG / data URL。

扫码登录的二维码内容就是 ``build_connect_url(task_id)`` 那一串地址。
三种呈现方式，按可用性降级：

* :func:`terminal_qr` —— 直接在终端里画（用半块字符 + ANSI 黑白配色，手机能扫）；
* :func:`qr_png_bytes` —— 存成 PNG，方便贴到聊天窗口或服务器上没有终端时用；
* :func:`qr_data_url` —— ``data:image/png;base64,…``，给本地 HTTP 登录页用。

``qrcode`` 是可选依赖：没装就退化成「打印链接 + 让用户自己复制到手机」，
**登录流程不会因为画不出二维码而失败**。
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

#: 纠错等级 M（约 15%）——二维码贴在终端里、可能被字体/抗锯齿糊掉，M 是常用折中。
_ERROR_CORRECTION = "M"

#: ANSI 256 色里的纯黑 / 纯白。
_BLACK = 16
_WHITE = 231


def has_qr_support() -> bool:
    """能不能画二维码（只要求 ``qrcode``，画 PNG 才额外要 Pillow）。"""
    return _qrcode() is not None


def has_png_support() -> bool:
    """能不能出 PNG（需要 ``qrcode`` + Pillow）。"""
    if _qrcode() is None:
        return False
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def support_note() -> str:
    """给人看的能力说明（doctor / 报错提示共用）。"""
    if has_png_support():
        return "终端二维码 + PNG 均可用"
    if has_qr_support():
        return "只装了 qrcode：可画终端二维码；装 Pillow 后还能出 PNG"
    return "没装 qrcode：只能打印链接（pip install qrcode）"


def qr_matrix(text: str) -> list[list[bool]] | None:
    """把文本编成二维码矩阵（含静默区）。``qrcode`` 缺失时返回 ``None``。"""
    qrcode = _qrcode()
    if qrcode is None:
        return None
    qr = qrcode.QRCode(
        version=None,
        error_correction=getattr(qrcode.constants, f"ERROR_CORRECT_{_ERROR_CORRECTION}"),
        box_size=1,
        border=2,
    )
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    return [[bool(cell) for cell in row] for row in matrix]


def terminal_qr(text: str, *, ansi: bool = True) -> str | None:
    """把二维码画成终端字符串。

    * ``ansi=True``：上半块字符 ``▀`` + 前景/背景色，得到**真正的黑白**二维码
      （一个字符代表竖着的两个模块，所以图形不会被拉长）；
    * ``ansi=False``：只用 ``█``/``▀``/``▄``/空格，适配不支持 ANSI 的终端。
    """
    matrix = qr_matrix(text)
    if matrix is None:
        return None
    rows = len(matrix)
    cols = len(matrix[0]) if rows else 0
    lines: list[str] = []
    for y in range(0, rows, 2):
        top = matrix[y]
        bottom = matrix[y + 1] if y + 1 < rows else [False] * cols
        if ansi:
            lines.append(_ansi_line(top, bottom))
        else:
            lines.append(
                "".join(_BLOCK[(bool(t), bool(b))] for t, b in zip(top, bottom, strict=True))
            )
    return "\n".join(lines)


def qr_png_bytes(text: str, *, scale: int = 8, border: int = 2) -> bytes | None:
    """渲染成 PNG 字节；缺 ``qrcode``/Pillow 时返回 ``None``。"""
    matrix = qr_matrix(text)
    if matrix is None:
        return None
    try:
        from PIL import Image
    except ImportError:
        return None

    size = len(matrix)
    side = (size + border * 2) * scale
    image = Image.new("L", (side, side), 255)
    pixels = image.load()
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if not dark:
                continue
            left = (x + border) * scale
            top = (y + border) * scale
            for dy in range(scale):
                for dx in range(scale):
                    pixels[left + dx, top + dy] = 0  # type: ignore[index]
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def qr_data_url(text: str) -> str | None:
    """``data:image/png;base64,…``。参考实现的 ``qrDataUrl`` 就是这个形状。"""
    payload = qr_png_bytes(text)
    if payload is None:
        return None
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


def save_png(text: str, path: str | Path) -> Path | None:
    """把二维码写成 PNG 文件；不支持时返回 ``None``（调用方据此给提示）。"""
    payload = qr_png_bytes(text)
    if payload is None:
        return None
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


# ---------------------------------------------------------------- 内部

#: 无 ANSI 时的四种组合：(上, 下) → 字符
_BLOCK = {
    (True, True): "█",
    (True, False): "▀",
    (False, True): "▄",
    (False, False): " ",
}


def _ansi_line(top: list[bool], bottom: list[bool]) -> str:
    """一行二维码 → 带 ANSI 颜色的半块字符。只在颜色变化时切换转义序列。"""
    parts: list[str] = []
    current: tuple[int, int] | None = None
    for t, b in zip(top, bottom, strict=True):
        colors = (_BLACK if t else _WHITE, _BLACK if b else _WHITE)
        if colors != current:
            parts.append(f"\x1b[38;5;{colors[0]};48;5;{colors[1]}m")
            current = colors
        parts.append("▀")
    parts.append("\x1b[0m")
    return "".join(parts)


def _qrcode() -> Any | None:
    try:
        import qrcode
    except ImportError:
        return None
    return qrcode


__all__ = [
    "has_png_support",
    "has_qr_support",
    "qr_data_url",
    "qr_matrix",
    "qr_png_bytes",
    "save_png",
    "support_note",
    "terminal_qr",
]
