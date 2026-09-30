"""Unicode 字素簇（grapheme cluster）计数工具。

使用第三方 regex 库的 \\X 转义按 UAX #29 字素簇边界切分，
例如 emoji 组合序列、组合附加符号、ZWJ 序列等都算作一个“字”。
"""
from __future__ import annotations

import regex as _regex

_GRAPHEME_RE = _regex.compile(r"\X")
# 去除常见的 VTT/HTML 内嵌标签（如 <c.class>、<i>、<b>、<u>、时间戳锚点等）
_TAG_RE = _regex.compile(r"<[^>]*>")
# 行内空白（含不间断空格等）不参与计数
_WS_RE = _regex.compile(r"\s")


def strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text)


def grapheme_len(text: str, *, strip_html: bool = True) -> int:
    """按 Unicode 字素簇统计字符数。默认忽略标签与所有空白。"""
    if text is None:
        return 0
    if strip_html:
        text = strip_tags(text)
    count = 0
    for _ in _GRAPHEME_RE.finditer(text):
        count += 1
    # \X 会把换行符也计为一个簇，这里剔除全部空白后再统计
    text_no_ws = _WS_RE.sub("", text)
    return sum(1 for _ in _GRAPHEME_RE.finditer(text_no_ws)) if text_no_ws else 0


def graphemes(text: str, *, strip_html: bool = True) -> list[str]:
    if strip_html:
        text = strip_tags(text)
    text = _WS_RE.sub("", text)
    return _GRAPHEME_RE.findall(text)
