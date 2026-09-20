"""二维码渲染测试。``qrcode`` 缺失时跳过（渲染降级本身也测了一条）。"""

from __future__ import annotations

import base64
import re

import pytest

from yuque_agent.qqbot import qr as qr_mod

pytest.importorskip("qrcode", reason="没装 qrcode：二维码相关的断言跳过")

TEXT = "https://q.qq.com/qqbot/openclaw/connect.html?task_id=abc&source=yuque-agent&_wv=2"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def test_terminal_qr_uses_half_blocks() -> None:
    art = qr_mod.terminal_qr(TEXT)
    assert art is not None
    lines = art.splitlines()
    assert len(lines) >= 10
    plain = strip_ansi(art)
    assert set(plain) <= {"▀", " ", "\n"}  # 字形只有上半块，颜色交给 ANSI
    assert "\x1b[38;5;" in art  # 确实是黑白配色，不是简单反色
    assert "▀" in plain


def test_terminal_qr_no_ansi_is_plain() -> None:
    art = qr_mod.terminal_qr(TEXT, ansi=False)
    assert art is not None
    assert "\x1b" not in art
    assert set(art) <= {"▀", "▄", "█", " ", "\n"}
    # 每行宽度一致（二维码是方的）
    widths = {len(line) for line in art.splitlines()}
    assert len(widths) == 1


def test_qr_matrix_is_square_and_bordered() -> None:
    matrix = qr_mod.qr_matrix(TEXT)
    assert matrix is not None
    assert len(matrix) == len(matrix[0])
    # 静默区（border=2）应该是干净的
    assert matrix[0] == [False] * len(matrix[0])


def test_qr_data_url_is_a_png_data_url() -> None:
    data_url = qr_mod.qr_data_url(TEXT)
    if data_url is None:  # 没有 Pillow
        pytest.skip("没装 Pillow，PNG 不可用")
    assert data_url.startswith("data:image/png;base64,")
    payload = base64.b64decode(data_url.split(",", 1)[1])
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"


def test_save_png(tmp_path) -> None:
    target = tmp_path / "sub" / "qr.png"
    written = qr_mod.save_png(TEXT, target)
    if written is None:
        pytest.skip("没装 Pillow，PNG 不可用")
    assert written.exists()
    assert written.stat().st_size > 100


def test_support_note_mentions_capability() -> None:
    note = qr_mod.support_note()
    assert "二维码" in note


def test_degrades_without_qrcode(monkeypatch) -> None:
    """没装 qrcode 时不能抛异常——登录流程要能退化成「打印链接」。"""
    monkeypatch.setattr(qr_mod, "_qrcode", lambda: None)
    assert qr_mod.has_qr_support() is False
    assert qr_mod.has_png_support() is False
    assert qr_mod.terminal_qr(TEXT) is None
    assert qr_mod.qr_png_bytes(TEXT) is None
    assert qr_mod.qr_data_url(TEXT) is None
    assert qr_mod.save_png(TEXT, "/tmp/should-not-exist.png") is None
