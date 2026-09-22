"""导入解析 → 冲突检测 → 校正提案 的编排层。"""
from __future__ import annotations

from dataclasses import dataclass

from .corrector import CueInput, shot_windows, solve_run
from .shots import Shot
from .subtitles import visible_text


@dataclass
class CueReport:
    uid: int
    cue_id: str
    ord: int
    start_ms: int
    end_ms: int
    duration_ms: int
    text: str
    graphemes: int
    raw_hash: str
    cps: float
    cps_ok: bool
    overlap_with: str | None = None      # 与前一条重叠/间隔不足
    cross_shot: bool = False             # 当前（原）位置跨镜头
    proposal_start_ms: int | None = None
    proposal_end_ms: int | None = None
    adopted: bool = False
    adopted_start_ms: int | None = None
    adopted_end_ms: int | None = None


@dataclass
class AnalysisResult:
    cues: list[CueReport]
    shots: list[tuple[int, int]]
    chains: list[dict]
    intrinsic: list[dict]
    stats: dict


def _cps(graphemes: int, duration_ms: int) -> float:
    if duration_ms <= 0:
        return float("inf")
    return graphemes * 1000.0 / duration_ms


def build_analysis(
    parsed: list[ParsedCue],
    shots_raw: list[Shot],
    cps_limit: float,
    min_gap_ms: int,
    tolerance_ms: int,
) -> AnalysisResult:
    shots = [(s.start_ms, s.end_ms) for s in shots_raw]
    bound = max(
        [c.end_ms for c in parsed] + [e for _, e in shots] + [1000]
    ) + tolerance_ms + 1000

    cue_inputs = [
        CueInput(c.cue_id, c.ord, c.start_ms, c.end_ms, c.graphemes)
        for c in parsed
    ]
    windows = [shot_windows(c, shots, tolerance_ms, bound) for c in cue_inputs]
    res = solve_run(cue_inputs, windows, min_gap_ms, bound)

    reports: list[CueReport] = []
    for i, pc in enumerate(parsed):
        ci = cue_inputs[i]
        dur = pc.end_ms - pc.start_ms
        cps = _cps(pc.graphemes, dur)
        overlap_with = None
        if i > 0:
            prev = parsed[i - 1]
            actual_gap = pc.start_ms - prev.end_ms
            if actual_gap < min_gap_ms:
                kind = "重叠" if actual_gap < 0 else "间隔不足"
                overlap_with = (
                    f"{kind}：与 {prev.cue_id} 的间隔为 {actual_gap}ms，"
                    f"小于最小间隔 {min_gap_ms}ms"
                )
        cross = len(windows[i]) == 0 and bool(shots)
        prop = res.proposals.get(i)
        reports.append(CueReport(
            uid=0,  # 由调用方（DB id）填充
            cue_id=pc.cue_id,
            ord=pc.ord,
            start_ms=pc.start_ms,
            end_ms=pc.end_ms,
            duration_ms=dur,
            text=visible_text(pc.raw_text),
            graphemes=pc.graphemes,
            raw_hash=pc.raw_hash,
            cps=round(cps, 2),
            cps_ok=cps <= cps_limit and dur > 0,
            overlap_with=overlap_with,
            cross_shot=cross,
            proposal_start_ms=prop,
            proposal_end_ms=(prop + dur) if prop is not None else None,
        ))

    chains = [{
        "cue_ids": [parsed[i].cue_id for i in ch.indexes],
        "indexes": ch.indexes,
        "reason": ch.reason,
    } for ch in res.chains]
    intrinsic = [{
        "cue_id": parsed[i].cue_id,
        "reason": reason,
    } for i, reason in sorted(res.intrinsic.items())]

    bad_gap = sum(1 for r in reports if r.overlap_with)
    bad_cps = sum(1 for r in reports if not r.cps_ok)
    moved = sum(1 for r in reports if r.proposal_start_ms is not None
                and r.proposal_start_ms != r.start_ms)
    stats = {
        "cue_count": len(reports),
        "shot_count": len(shots),
        "gap_violations": bad_gap,
        "cps_violations": bad_cps,
        "cross_shot_count": sum(1 for r in reports if r.cross_shot),
        "proposal_count": len(res.proposals),
        "proposal_moved_count": moved,
        "conflict_chain_count": len(chains),
        "conflict_cue_count": sum(len(c["indexes"]) for c in chains),
        "total_shift_ms": sum(
            abs(reports[i].proposal_start_ms - cue_inputs[i].start_ms)
            for i in res.proposals
        ),
    }
    return AnalysisResult(
        cues=reports, shots=shots, chains=chains,
        intrinsic=intrinsic, stats=stats,
    )
