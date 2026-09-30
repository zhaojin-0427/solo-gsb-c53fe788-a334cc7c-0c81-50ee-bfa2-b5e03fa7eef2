"""求解器与解析、检测模块的端到端校验脚本（不依赖 pytest）。"""
import itertools
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import analyzer, parsers, solver
from app.grapheme import grapheme_len

SETT = {"max_cps": 20.0, "min_gap_ms": 40, "boundary_tolerance_ms": 120}
fails = []


def check(name, cond, extra=""):
    print(("PASS" if cond else "FAIL"), name, extra)
    if not cond:
        fails.append(name)


def brute_force(cues, shots, settings, rng):
    """小范围暴力枚举，校验三级字典序最优。"""
    gap = settings["min_gap_ms"]
    tol = settings["boundary_tolerance_ms"]
    ordered = sorted(cues, key=lambda c: (c["start_ms"], c["end_ms"], c["cue_id"]))
    a = [c["start_ms"] for c in ordered]
    d = [c["end_ms"] - c["start_ms"] for c in ordered]
    ids = [c["cue_id"] for c in ordered]

    def feasible(z):
        for i, zi in enumerate(z):
            if zi < 0:
                return False
            if shots:
                if not any(s["start_ms"] - tol <= zi and zi + d[i] <= s["end_ms"] + tol
                           for s in shots):
                    return False
            if i > 0 and zi < z[i - 1] + d[i - 1] + gap:
                return False
        return True

    best = None
    ranges = [range(ai - rng, ai + rng + 1) for ai in a]
    for combo in itertools.product(*ranges):
        if not feasible(combo):
            continue
        l1 = sum(abs(combo[i] - a[i]) for i in range(len(a)))
        moved_set = {ids[i] for i in range(len(a)) if combo[i] != a[i]}
        l2 = len(moved_set)
        all_ids = sorted(ids)
        # L3：按 cue_id 升序的“是否移动”指示向量字典序最小
        vec = tuple(1 if cid in moved_set else 0 for cid in all_ids)
        key = (l1, l2, vec)
        if best is None or key < best[0]:
            best = (key, combo, moved_set)
    return best


def run_case(name, cues, shots, rng=60, expect_feasible=None):
    res = solver.solve(cues, shots, SETT)
    # 校验输出合法性
    by_id = {c["cue_id"]: c for c in cues}
    z = {}
    for it in res["items"]:
        c = by_id[it["cue_id"]]
        dur = c["end_ms"] - c["start_ms"]
        assert it["duration_ms"] == dur, "时长被改变"
        assert it["corrected_end_ms"] - it["corrected_start_ms"] == dur
        z[it["cue_id"]] = (it["corrected_start_ms"], dur, it)
    ordered = res["items"]
    ok_constraints = True
    tol, gap = SETT["boundary_tolerance_ms"], SETT["min_gap_ms"]
    for i, it in enumerate(ordered):
        zi, dur, _ = z[it["cue_id"]]
        if it["status"] not in ("conflict", "cps_conflict"):
            if zi < 0:
                ok_constraints = False
            if shots and not any(s["start_ms"] - tol <= zi and zi + dur <= s["end_ms"] + tol
                                 for s in shots):
                ok_constraints = False
            if i > 0:
                pj = ordered[i - 1]
                pz, pd, _ = z[pj["cue_id"]]
                if pj["status"] not in ("conflict", "cps_conflict") and zi < pz + pd + gap - 1e-9:
                    ok_constraints = False
    check(name + " 约束满足", ok_constraints)

    # 暴力比对（仅对无 CPS 障碍的小例子）
    cps_bad = any(it["status"] == "cps_conflict" for it in ordered)
    if not cps_bad and len(cues) <= 4:
        bf = brute_force(cues, shots, SETT, rng)
        if bf is None:
            check(name + " 暴力判定不可行一致", not res["feasible"],
                  f"solver feasible={res['feasible']}")
        else:
            got_l1 = res["stats"]["total_abs_shift_ms"]
            got_moved = {it["cue_id"] for it in ordered if it["delta_ms"] != 0}
            all_ids = sorted(by_id)
            got_vec = tuple(1 if cid in got_moved else 0 for cid in all_ids)
            got_key = (got_l1, len(got_moved), got_vec)
            same = got_key == bf[0]
            check(name + " 与暴力最优一致(L1/L2/L3)", same,
                  f"solver={got_key} brute={bf[0]} z={[it['corrected_start_ms'] for it in ordered]}")
    if expect_feasible is not None:
        check(name + " 可行性预期", res["feasible"] == expect_feasible)
    return res


# 1. 字素簇计数
check("grapheme ASCII", grapheme_len("hello") == 5)
check("grapheme 中文", grapheme_len("你好世界") == 4)
check("grapheme emoji ZWJ", grapheme_len("👨‍👩‍👧‍👦") == 1)
check("grapheme 组合音标", grapheme_len("é") == 1)
check("grapheme 肤色修饰", grapheme_len("👍🏽") == 1)
check("grapheme 去标签去空白", grapheme_len("<i>hi there</i>\n") == 7)

# 2. 解析
srt = open(os.path.join(os.path.dirname(__file__), "..", "examples", "sample.srt"),
           encoding="utf-8").read()
sub = parsers.parse_subtitle(srt, "sample.srt")
check("srt 解析条数", len(sub["cues"]) == 5)
check("srt cue_id", sub["cues"][0]["cue_id"] == "1")
check("srt 时间码", sub["cues"][0]["start_ms"] == 1200 and sub["cues"][1]["start_ms"] == 3480)

vtt = """WEBVTT

00:01.000 --> 00:02.500
第一行

cue-2
00:03.000 --> 00:04.000
第二行 👨‍👩‍👧‍👦
"""
v = parsers.parse_subtitle(vtt, "x.vtt")
check("vtt 解析", len(v["cues"]) == 2 and v["format"] == "vtt")
check("vtt emoji 计数", v["cues"][1]["grapheme_count"] == 4)

shots = parsers.parse_shots(open(os.path.join(os.path.dirname(__file__), "..", "examples",
                                              "shots.json"), encoding="utf-8").read())
check("shots 解析", len(shots) == 5 and shots[0]["start_ms"] == 0)

# 3. 检测
issues = analyzer.analyze(sub["cues"], shots, SETT)
flat = [i for iss in issues for i in iss]
check("analyze 检出重叠(cue1/2)", any(i["type"] == "overlap" for i in flat))
check("analyze 检出跨镜头(cue3)", any(i["type"] == "cross_shot" and i["severity"] == "error"
                                      for i in issues[2]))
check("analyze CPS cue4", any(i["type"] == "cps" for i in issues[3]))

# 4. 求解器各场景
C = lambda cid, s, e, t: {"cue_id": cid, "start_ms": s, "end_ms": e, "text": t,
                          "grapheme_count": grapheme_len(t)}

# 4a. 无需改动
run_case("already-ok",
         [C("a", 1000, 2000, "没问题的一条"), C("b", 2500, 3500, "另外的一条呀")],
         [{"shot_id": "s1", "start_ms": 0, "end_ms": 5000}])

# 4b. 重叠，需要平移
run_case("overlap-shift",
         [C("a", 1000, 2000, "第一条字幕"), C("b", 1900, 2600, "第二条字幕")],
         [{"shot_id": "s1", "start_ms": 0, "end_ms": 10000}],
         rng=300)

# 4c. 镜头约束：cue 横跨切点，需推入某个镜头
run_case("shot-boundary",
         [C("a", 3400, 3600, "跨边界的小条")],
         [{"shot_id": "s1", "start_ms": 0, "end_ms": 3500},
          {"shot_id": "s2", "start_ms": 3500, "end_ms": 8000}],
         rng=300)

# 4d. 不可行：两条长 cue 挤在一个短镜头内
r = run_case("infeasible-chain",
             [C("a", 0, 2000, "第一条很长的内容啊啊啊"),
              C("b", 100, 2200, "第二条也很长的内容哦")],
             [{"shot_id": "s1", "start_ms": 0, "end_ms": 3000}],
             expect_feasible=False)
check("infeasible 冲突链", any(c["type"] == "timing_chain" for c in r["conflict_chains"]))

# 4e. CPS 障碍固定，后续 cue 需要绕开
r = run_case("cps-obstacle",
             [C("a", 0, 500, "超级超级长的一行根本读不完啊啊啊啊啊啊啊"),
              C("b", 400, 1000, "后面正常的一条字幕")],
             [{"shot_id": "s1", "start_ms": 0, "end_ms": 5000}],
             expect_feasible=False)
check("cps 障碍标记", r["items"][0]["status"] == "cps_conflict")
check("cps 冲突链类型",
      any(c["type"] == "cps_intrinsic" for c in r["conflict_chains"]))

# 4f. L3 字典序：两条等价可动时，应移动 cue_id 较大的
r = run_case("lex-order",
             [C("aaa", 1000, 1500, "甲"),
              C("bbb", 1490, 1900, "乙")],
             [{"shot_id": "s1", "start_ms": 0, "end_ms": 5000}])
moved = [it["cue_id"] for it in r["items"] if it["delta_ms"] != 0]
check("L3 移动较大的 cue_id", moved == ["bbb"], str(moved))

# 4g. 无镜头模式
run_case("no-shots",
         [C("a", 1000, 2000, "字幕甲"), C("b", 1950, 3000, "字幕乙")],
         [], rng=200)

# 4h. 多链条：左移与右移的 L1 取舍
run_case("l1-tradeoff",
         [C("a", 1000, 1600, "中"), C("b", 1590, 2200, "文"), C("c", 2190, 2800, "条")],
         [{"shot_id": "s1", "start_ms": 0, "end_ms": 10000}],
         rng=100)

# 5. 示例文件全流程
r = solver.solve(sub["cues"], shots, SETT)
print("sample solve stats:", r["stats"])
print("chains:", [(c["type"], c.get("pilot") or c.get("pivot"), c["cue_ids"]) for c in r["conflict_chains"]])

print()
if fails:
    print(f"{len(fails)} 项失败：", fails)
    sys.exit(1)
print("全部测试通过 ✅")
