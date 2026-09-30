"""问题检测：重叠、空隙、每秒字符数（CPS）、跨镜头切换。

所有字符计数均按 Unicode 字素簇（app.grapheme）。
"""
from __future__ import annotations

from .grapheme import grapheme_len

# 超过该空隙（毫秒）给出“长空隙”信息提示（不构成违规，也不参与校正）
LARGE_GAP_MS = 3000


def _cps(glen: int, start_ms: int, end_ms: int) -> float:
    dur = (end_ms - start_ms) / 1000.0
    if dur <= 0:
        return float("inf")
    return glen / dur


def _shot_enclosing(shots: list[dict], start_ms: int, end_ms: int, tol: int) -> dict | None:
    """是否存在镜头 [S,E] 使 S-tol ≤ start 且 end ≤ E+tol（与求解器同一语义）。"""
    for s in shots:
        if s["start_ms"] - tol <= start_ms and end_ms <= s["end_ms"] + tol:
            return s
    return None


def analyze(cues: list[dict], shots: list[dict], settings: dict) -> list[dict]:
    """返回每个 cue 的问题列表，顺序与 cues 相同（cues 需已按开始时间排序）。"""
    max_cps = float(settings["max_cps"])
    min_gap = int(settings["min_gap_ms"])
    tol = int(settings["boundary_tolerance_ms"])

    issues_by_id: dict[str, list[dict]] = {c["cue_id"]: [] for c in cues}

    for c in cues:
        cid = c["cue_id"]
        dur = c["end_ms"] - c["start_ms"]
        # CPS
        cps = _cps(c.get("grapheme_count", grapheme_len(c["text"])), c["start_ms"], c["end_ms"])
        if cps > max_cps + 1e-9:
            issues_by_id[cid].append({
                "type": "cps",
                "severity": "error",
                "message": f"每秒 {cps:.2f} 字，超过上限 {max_cps:g}（{c.get('grapheme_count')} 字 / {dur} ms）；平移不改变时长，无法通过平移修复",
                "cps": round(cps, 3),
            })

        # 跨镜头：与求解器同一语义——存在镜头使两端在容差内贴合即为安全
        if shots:
            inside = _shot_enclosing(shots, c["start_ms"], c["end_ms"], tol)
            crossed = [s for s in shots
                       if c["start_ms"] < s["start_ms"] < c["end_ms"]
                       or c["start_ms"] < s["end_ms"] < c["end_ms"]]
            if inside is not None:
                if crossed:
                    issues_by_id[cid].append({
                        "type": "cross_shot",
                        "severity": "info",
                        "message": f"跨越镜头边界但在容差 ±{tol} ms 内贴合镜头"
                                   f" {inside['shot_id']}，视为安全",
                        "shot_ids": [s["shot_id"] for s in crossed],
                    })
            elif crossed:
                issues_by_id[cid].append({
                    "type": "cross_shot",
                    "severity": "error",
                    "message": f"字幕跨越镜头切换点且超出边界容差 ±{tol} ms"
                               f"（镜头：{', '.join(s['shot_id'] for s in crossed[:5])}"
                               + (" …" if len(crossed) > 5 else "") + "）",
                    "shot_ids": [s["shot_id"] for s in crossed],
                })

    # 相邻关系：重叠 / 最小间隔 / 长空隙
    for i in range(1, len(cues)):
        prev, cur = cues[i - 1], cues[i]
        gap = cur["start_ms"] - prev["end_ms"]
        if gap < 0:
            msg = f"与前一条 {prev['cue_id']} 重叠 {-gap} ms"
            issues_by_id[cur["cue_id"]].append({
                "type": "overlap", "severity": "error", "message": msg,
                "other_cue_id": prev["cue_id"], "gap_ms": gap,
            })
            issues_by_id[prev["cue_id"]].append({
                "type": "overlap", "severity": "error",
                "message": f"与后一条 {cur['cue_id']} 重叠 {-gap} ms",
                "other_cue_id": cur["cue_id"], "gap_ms": gap,
            })
        elif gap < min_gap:
            msg = f"与前一条 {prev['cue_id']} 间隔仅 {gap} ms（最小 {min_gap} ms）"
            issues_by_id[cur["cue_id"]].append({
                "type": "gap_small", "severity": "error", "message": msg,
                "other_cue_id": prev["cue_id"], "gap_ms": gap,
            })
            issues_by_id[prev["cue_id"]].append({
                "type": "gap_small", "severity": "error",
                "message": f"与后一条 {cur['cue_id']} 间隔仅 {gap} ms（最小 {min_gap} ms）",
                "other_cue_id": cur["cue_id"], "gap_ms": gap,
            })
        elif gap > LARGE_GAP_MS:
            issues_by_id[cur["cue_id"]].append({
                "type": "gap_large", "severity": "info",
                "message": f"与前一条 {prev['cue_id']} 间隔 {gap} ms（长空隙，仅提示）",
                "other_cue_id": prev["cue_id"], "gap_ms": gap,
            })

    return [issues_by_id[c["cue_id"]] for c in cues]
