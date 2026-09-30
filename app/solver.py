"""确定性校正求解器。

规则（严格只平移，保持每条 cue 的原时长 d_i 不变）：
  第 i 条（按 (start,end,cue_id) 排序）平移后起点 z_i，原起点 a_i，Δ_i = z_i-a_i。
  约束：
    1) 相邻：z_i ≥ z_{i-1} + d_{i-1} + g（g=最小间隔）
    2) 镜头安全：存在镜头 [S,E]，使 S-tol ≤ z_i 且 z_i+d_i ≤ E+tol；无镜头数据时不施加
    3) z_i ≥ 0
  CPS 仅取决于时长，平移无法修复：CPS 超限的 cue 作为“固定障碍”（Δ=0），
  仍参与相邻约束，本身单列为 cps_intrinsic 冲突链。

择优（逐级字典序，整数毫秒，完全确定性）：
  L1  Σ|Δ_i| 总移动毫秒最少
  L2  改动条数（Δ_i≠0）最少
  L3  以 cue_id 升序排列的“是否移动”位向量字典序最小
编码：score = L1·W1 + 改动数·W2 + Σ(移动?2^(M-1-rank):0)，
W2 > 2^M，W1 > M·W2 + 2^M，Python 大整数保证不进位。

最优性：在 y_i = z_i - P_i（P_i 为 d+g 累积）变换下问题为单调链
  y_1 ≤ … ≤ y_m，各 y_i 属于并集区间，目标 Σ|y_i-t_i|（t_i=a_i-P_i），
即“带盒约束的 L1 保序回归”；存在最优解使每个 y_i 取域端点或某 t_j
（另加 t_j±B 裁剪点，B=最早排程的 Σ|Δ| 上界），在该有限候选集上做
前缀最小分层 DP 即得全局最优。不可行的段输出冲突链。
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right

from .grapheme import grapheme_len

INF_UPPER = 10**15


# ---------------------------------------------------------------------------
# 可行域（并集区间，已按起点排序并合并）
# ---------------------------------------------------------------------------

def shot_windows(duration_ms: int, shots: list[dict], tol: int) -> list[tuple[int, int]]:
    """cue 起点 z 的镜头安全区间并集（裁剪到 z≥0）。"""
    wins: list[tuple[int, int]] = []
    for s in shots:
        lo = max(s["start_ms"] - tol, 0)
        hi = s["end_ms"] - duration_ms + tol
        if lo <= hi:
            wins.append((lo, hi))
    wins.sort()
    merged: list[tuple[int, int]] = []
    for lo, hi in wins:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def _pick_ge(windows, lower: int) -> int | None:
    """域中 ≥ lower 的最小值；windows=None 表示 [0,∞)。"""
    if windows is None:
        return max(lower, 0)
    idx = bisect_right(windows, (lower, 10**18)) - 1
    if idx >= 0 and windows[idx][1] >= lower:
        lo = windows[idx][0]
        return max(lo, lower)
    idx += 1
    if idx < len(windows):
        return windows[idx][0]
    return None


def _pick_le(windows, upper: int) -> int | None:
    """域中 ≤ upper 的最大值。"""
    if windows is None:
        return upper if upper >= 0 else None
    idx = bisect_right(windows, (upper, 10**18)) - 1
    if idx < 0:
        return None
    lo, hi = windows[idx]
    return min(hi, upper) if lo <= upper else None


def _contains(windows, v: int) -> bool:
    if windows is None:
        return v >= 0
    idx = bisect_right(windows, (v, 10**18)) - 1
    return idx >= 0 and windows[idx][0] <= v <= windows[idx][1]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def solve(cues: list[dict], shots: list[dict], settings: dict,
          original_error_ids: set[str] | None = None) -> dict:
    max_cps = float(settings["max_cps"])
    gap = int(settings["min_gap_ms"])
    tol = int(settings["boundary_tolerance_ms"])

    ordered = sorted(cues, key=lambda c: (c["start_ms"], c["end_ms"], c["cue_id"]))
    n = len(ordered)
    if n == 0:
        return {"items": [], "conflict_chains": [], "stats": _stats([]), "feasible": True}

    a = [c["start_ms"] for c in ordered]
    d = [c["end_ms"] - c["start_ms"] for c in ordered]
    cids = [c["cue_id"] for c in ordered]
    glens = [c.get("grapheme_count", grapheme_len(c["text"])) for c in ordered]
    cps_bad = [(glens[i] / (d[i] / 1000.0)) > max_cps + 1e-9 if d[i] > 0 else True
               for i in range(n)]

    windows = [None if not shots else shot_windows(d[i], shots, tol) for i in range(n)]
    fixed = cps_bad[:]

    # P_i：累积偏移；链约束等价于 y_i ≥ y_{i-1}
    P = [0] * n
    for i in range(1, n):
        P[i] = P[i - 1] + d[i - 1] + gap

    result_delta: list[int | None] = [0 if fixed[i] else None for i in range(n)]
    status: list[str] = ["cps_conflict" if fixed[i] else "" for i in range(n)]
    conflict_chains: list[dict] = []

    for i in range(n):
        if cps_bad[i]:
            cps = glens[i] / (d[i] / 1000.0) if d[i] > 0 else float("inf")
            conflict_chains.append({
                "type": "cps_intrinsic",
                "reason": f"每秒 {cps:.2f} 字（{glens[i]} 字 / {d[i]} ms），"
                          f"超过上限 {max_cps:g}；平移保持时长，无法修复",
                "cue_ids": [cids[i]],
                "roles": {cids[i]: "cps_intrinsic"},
            })

    # 固定障碍之间的连续可移动段
    segments: list[tuple[int, int]] = []
    i = 0
    while i < n:
        if fixed[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and not fixed[j + 1]:
            j += 1
        segments.append((i, j))
        i = j + 1

    feasible_all = not any(cps_bad)

    for lo, hi in segments:
        seg = list(range(lo, hi + 1))
        m = len(seg)
        left_limit = a[lo - 1] + d[lo - 1] + gap if lo > 0 else 0
        has_left_wall = lo > 0
        right_limit = a[hi + 1] - d[hi] - gap if hi + 1 < n else None
        has_right_wall = hi + 1 < n

        # ---- 前向最早排程 ----
        fwd: list[int] = []
        fail_at = -1
        for k, i in enumerate(seg):
            if k == 0:
                lower = left_limit if has_left_wall else 0
            else:
                lower = fwd[-1] + d[seg[k - 1]] + gap
            v = _pick_ge(windows[i], lower)
            if v is None or (k == m - 1 and has_right_wall and v > right_limit):
                fail_at = k
                break
            fwd.append(v)

        # ---- 反向最晚排程 ----
        bwd: list[int] = [0] * m
        fail_b = -1
        upper = right_limit if has_right_wall else INF_UPPER
        for k in range(m - 1, -1, -1):
            v = _pick_le(windows[seg[k]], upper)
            if v is None or (k == 0 and has_left_wall and v < left_limit):
                fail_b = k
                break
            bwd[k] = v
            if k > 0:
                # z_{i-1} + d_{seg[k-1]} + gap ≤ z_i
                upper = v - d[seg[k - 1]] - gap

        clash = -1
        if fail_at < 0 and fail_b < 0:
            for k in range(m):
                if fwd[k] > bwd[k]:
                    clash = k
                    break

        if fail_at >= 0 or fail_b >= 0 or clash >= 0:
            feasible_all = False
            if fail_at >= 0:
                reason = (f"前向排到第 {fail_at + 1} 条“{cids[seg[fail_at]]}”时，"
                          f"不存在不早于前条结束 + {gap} ms 且落在某镜头内（含容差）的起点")
                pivot = cids[seg[fail_at]]
            elif fail_b >= 0:
                reason = (f"反向排到“{cids[seg[fail_b]]}”时，"
                          f"不存在不越过右边界且落在某镜头内（含容差）的起点")
                pivot = cids[seg[fail_b]]
            else:
                reason = (f"最早排程与最晚排程在“{cids[seg[clash]]}”附近交叉："
                          f"左侧链条要求的最早起点晚于右侧链条允许的最晚起点")
                pivot = cids[seg[clash]]
            ids, roles = [], {}
            if has_left_wall:
                ids.append(cids[lo - 1]); roles[cids[lo - 1]] = "fixed_left"
            for i in seg:
                ids.append(cids[i]); roles[cids[i]] = "member"
            if has_right_wall:
                ids.append(cids[hi + 1]); roles[cids[hi + 1]] = "fixed_right"
            conflict_chains.append({
                "type": "timing_chain",
                "reason": reason,
                "pivot": pivot,
                "cue_ids": ids,
                "roles": roles,
            })
            for i in seg:
                status[i] = "conflict"
            continue

        # ---- 可行：分层 DP 求三级字典序最优 ----
        B = sum(abs(fwd[k] - a[seg[k]]) for k in range(m))
        sorted_ids = sorted(cids[i] for i in seg)
        rank = {cid: r for r, cid in enumerate(sorted_ids)}
        w3 = {cid: (1 << (m - 1 - rank[cid])) for cid in sorted_ids}
        W2 = (1 << m) + 1
        W1 = m * W2 + (1 << m) + 1

        def edge(delta: int, cid: str) -> int:
            if delta == 0:
                return 0
            return abs(delta) * W1 + W2 + w3[cid]

        sources_all = []
        for i in seg:
            sources_all.append(a[i] - P[i])
        sources_all.sort()

        layers: list[list[int]] = []
        for k, i in enumerate(seg):
            t = a[i] - P[i]
            ylo, yhi = t - B, t + B
            if k == 0:
                ylo = max(ylo, (left_limit if has_left_wall else 0) - P[i])
            if k == m - 1 and has_right_wall:
                yhi = min(yhi, right_limit - P[i])
            # 源点取全部 t_j，由下方裁剪框与域判定其可达性
            cand = {y for y in sources_all
                    if ylo <= y <= yhi and _contains(windows[i], y + P[i])}
            # 每个域窗口与 [ylo,yhi] 的裁剪端点（仅相交窗口）
            zlo, zhi = ylo + P[i], yhi + P[i]
            if windows[i] is None:
                win_iter = [(max(0, zlo), INF_UPPER)]
            else:
                wl = bisect_left(windows[i], (zlo, -10**18))
                wr = bisect_right(windows[i], (zhi, 10**18))
                win_iter = windows[i][max(0, wl - 1):wr]
            for wlo, whi in win_iter:
                clo = max(wlo - P[i], ylo)
                chi = min(whi - P[i], yhi)
                if clo <= chi:
                    cand.add(clo)
                    cand.add(chi)
            layers.append(sorted(cand))

        # 第一层
        prev_costs: list[int] = []
        for y in layers[0]:
            delta = y + P[seg[0]] - a[seg[0]]
            prev_costs.append(edge(delta, cids[seg[0]]))
        parents: list[list[int | None]] = [[None] * len(layers[0])]

        for k in range(1, m):
            prev_y = layers[k - 1]
            pref_cost: list[int | None] = []
            pref_idx: list[int | None] = []
            best_c, best_i = None, -1
            for idx, cst in enumerate(prev_costs):
                if cst is not None and (best_c is None or cst < best_c):
                    best_c, best_i = cst, idx
                pref_cost.append(best_c)
                pref_idx.append(best_i)
            new_costs: list[int | None] = []
            par: list[int | None] = []
            i = seg[k]
            for y in layers[k]:
                p = bisect_right(prev_y, y) - 1  # 单调约束 y_{k-1} ≤ y_k
                if p < 0 or pref_cost[p] is None:
                    new_costs.append(None)
                    par.append(None)
                else:
                    delta = y + P[i] - a[i]
                    new_costs.append(pref_cost[p] + edge(delta, cids[i]))
                    par.append(pref_idx[p])
            prev_costs = new_costs
            parents.append(par)

        end = [(cst, idx) for idx, cst in enumerate(prev_costs) if cst is not None]
        if not end:
            feasible_all = False
            for i in seg:
                status[i] = "conflict"
            conflict_chains.append({
                "type": "timing_chain",
                "reason": "DP 无可达终点（理论上不应出现，可能是候选集构造缺陷）",
                "cue_ids": [cids[i] for i in seg],
                "roles": {cids[i]: "member" for i in seg},
            })
            continue
        _, end_idx = min(end)
        chosen = [0] * m
        chosen[-1] = layers[-1][end_idx]
        p = parents[-1][end_idx]
        for k in range(m - 2, -1, -1):
            chosen[k] = layers[k][p]
            p = parents[k][p]
        for k, i in enumerate(seg):
            result_delta[i] = chosen[k] + P[i] - a[i]

    # ---- 组装 ----
    err = original_error_ids or set()
    items: list[dict] = []
    for i, c in enumerate(ordered):
        delta = result_delta[i] if result_delta[i] is not None else 0
        if fixed[i]:
            st = "cps_conflict"
        elif status[i] == "conflict":
            st = "conflict"
        elif delta != 0:
            st = "corrected"
        elif c["cue_id"] in err:
            st = "corrected"
        else:
            st = "unmodified"
        items.append({
            "cue_id": c["cue_id"],
            "order": i,
            "original_start_ms": a[i],
            "original_end_ms": c["end_ms"],
            "corrected_start_ms": a[i] + delta,
            "corrected_end_ms": c["end_ms"] + delta,
            "delta_ms": delta,
            "duration_ms": d[i],
            "grapheme_count": glens[i],
            "text": c["text"],
            "status": st,
            "adopted": False,
        })

    return {
        "items": items,
        "conflict_chains": conflict_chains,
        "stats": _stats(items),
        "feasible": feasible_all,
    }


def _stats(items: list[dict]) -> dict:
    movable = [it for it in items
               if it["status"] in ("corrected", "unmodified")]
    return {
        "cue_count": len(items),
        "moved_count": sum(1 for it in movable if it["delta_ms"] != 0),
        "changed_count": sum(1 for it in movable if it["delta_ms"] != 0),
        "total_abs_shift_ms": sum(abs(it["delta_ms"]) for it in movable),
        "conflict_count": sum(1 for it in items if it["status"] == "conflict"),
        "cps_conflict_count": sum(1 for it in items if it["status"] == "cps_conflict"),
    }
