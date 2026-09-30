"""SRT / WebVTT 解析与镜头 JSON 解析。"""
from __future__ import annotations

import json
import re

from .grapheme import grapheme_len

# ---------------------------------------------------------------------------
# 时间码
# ---------------------------------------------------------------------------

_TS = re.compile(
    r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[,.](\d{1,3})"
)


def parse_timestamp(token: str) -> int:
    """把 HH:MM:SS,mmm / MM:SS.mmm 解析为毫秒。"""
    m = _TS.search(token.strip())
    if not m:
        raise ValueError(f"无法解析时间码: {token!r}")
    hh, mm, ss, ms = m.groups()
    hh = int(hh or 0)
    mm = int(mm)
    ss = int(ss)
    ms = int(ms.ljust(3, "0"))
    return hh * 3600_000 + mm * 60_000 + ss * 1000 + ms


def format_timestamp(ms: int, *, sep: str = ",") -> str:
    """格式化为 HH:MM:SS,mmm。"""
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{milli:03d}"


# ---------------------------------------------------------------------------
# SRT / VTT
# ---------------------------------------------------------------------------

def _parse_cues(content: str, *, is_vtt: bool) -> list[dict]:
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    # UTF-8 BOM
    if content.startswith("﻿"):
        content = content[1:]
    blocks = re.split(r"\n[ \t]*\n", content.strip("\n"))
    cues: list[dict] = []
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip() != ""]
        if not lines:
            continue
        # 找到时间行
        time_idx = -1
        for i, ln in enumerate(lines):
            if "-->" in ln:
                time_idx = i
                break
        if time_idx < 0:
            # VTT 头部 STYLE/REGION/NOTE 等整块忽略
            continue
        time_line = lines[time_idx]
        left, right = time_line.split("-->", 1)
        # VTT 右侧可能带 settings: 00:00:01.000 --> 00:00:02.000 align:start
        right = right.split()[0]
        start = parse_timestamp(left)
        end = parse_timestamp(right)
        text_lines = lines[time_idx + 1:]
        if not text_lines:
            continue
        # NOTE 块排除（VTT）
        if is_vtt and any(l.strip().startswith(("NOTE", "REGION", "STYLE")) for l in lines[:time_idx + 1]):
            continue
        text = "\n".join(text_lines)
        if end < start:
            raise ValueError(f"字幕结束时间早于开始时间: {left.strip()} --> {right}")
        cues.append({
            "start_ms": start,
            "end_ms": end,
            "text": text,
            "source_index": len(cues),
        })
    if not cues:
        raise ValueError("未解析到任何字幕条目（请检查 SRT / VTT 格式）")
    return cues


def parse_subtitle(content: str, filename: str | None = None) -> dict:
    """解析 SRT 或 WebVTT。返回 {"format": ..., "cues": [...]}。

    cue_id 取 SRT 序号（若存在），否则使用 cue-0001 形式；重复时追加后缀。
    """
    head = content[:64].upper().replace("﻿", "")
    is_vtt = "WEBVTT" in head
    if filename and filename.lower().endswith(".vtt"):
        is_vtt = True
    raw_cues = _parse_cues(content, is_vtt=is_vtt)

    # 尝试从原文提取 SRT 序号
    content_norm = content.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n[ \t]*\n", content_norm.strip("\n"))
    seq_numbers: list[str | None] = []
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip() != ""]
        if not lines or "--" not in "\n".join(lines):
            continue
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), -1)
        if idx < 0:
            continue
        if idx == 1 and re.fullmatch(r"\d{1,20}", lines[0].strip()):
            seq_numbers.append(lines[0].strip())
        else:
            seq_numbers.append(None)
    seq_iter = iter(seq_numbers)

    seen: set[str] = set()
    cues: list[dict] = []
    for n, c in enumerate(raw_cues, start=1):
        sid = next(seq_iter, None) if len(seq_numbers) == len(raw_cues) else None
        base = sid if sid else f"cue-{n:04d}"
        cue_id = base
        suffix = 2
        while cue_id in seen:
            cue_id = f"{base}__{suffix}"
            suffix += 1
        seen.add(cue_id)
        text = c["text"]
        cues.append({
            "cue_id": cue_id,
            "start_ms": c["start_ms"],
            "end_ms": c["end_ms"],
            "text": text,
            "grapheme_count": grapheme_len(text),
        })
    return {"format": "vtt" if is_vtt else "srt", "cues": cues}


# ---------------------------------------------------------------------------
# 镜头 JSON
# ---------------------------------------------------------------------------

def _to_ms(v) -> int:
    if isinstance(v, bool):
        raise ValueError("时间不能为布尔值")
    if isinstance(v, (int, float)):
        return int(round(v))
    if isinstance(v, str):
        return parse_timestamp(v)
    raise ValueError(f"无法识别的时间值: {v!r}")


def parse_shots(content: str) -> list[dict]:
    """解析镜头 JSON。

    支持：
    - [{"start": .., "end": .., "id": ..}, ...]
    - {"shots": [...]}
    键名兼容 start_ms/startMs/in/from、end_ms/endMs/out/to。
    """
    data = json.loads(content)
    if isinstance(data, dict) and "shots" in data:
        data = data["shots"]
    if not isinstance(data, list):
        raise ValueError("镜头 JSON 需为数组或含 shots 数组的对象")
    shots: list[dict] = []
    start_keys = ("start_ms", "startMs", "start", "in", "from")
    end_keys = ("end_ms", "endMs", "end", "out", "to")
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"第 {i + 1} 个镜头不是对象")
        sk = next((k for k in start_keys if k in item), None)
        ek = next((k for k in end_keys if k in item), None)
        if sk is None or ek is None:
            raise ValueError(f"第 {i + 1} 个镜头缺少起止字段")
        start = _to_ms(item[sk])
        end = _to_ms(item[ek])
        if end < start:
            raise ValueError(f"第 {i + 1} 个镜头结束早于开始")
        shot_id = item.get("id") or item.get("shot_id") or item.get("name") or f"shot-{i + 1:04d}"
        shots.append({
            "shot_id": str(shot_id),
            "start_ms": start,
            "end_ms": end,
        })
    shots.sort(key=lambda s: (s["start_ms"], s["end_ms"]))
    # 重新规范重复 id
    seen: set[str] = set()
    for i, s in enumerate(shots, 1):
        base = s["shot_id"]
        sid, n = base, 2
        while sid in seen:
            sid = f"{base}__{n}"
            n += 1
        seen.add(sid)
        s["shot_id"] = sid
    return shots
