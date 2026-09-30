"""字幕节奏与镜头边界校正台 —— FastAPI 服务端。"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analyzer, db, parsers, solver

app = FastAPI(title="字幕节奏与镜头边界校正台", version="1.0.0")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class SettingsIn(BaseModel):
    max_cps: float = Field(gt=0, le=1000)
    min_gap_ms: int = Field(ge=0, le=60000)
    boundary_tolerance_ms: int = Field(ge=0, le=60000)


class AdoptIn(BaseModel):
    adopted: bool


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _ordered_cues(conn):
    cues = db.load_cues(conn)
    return sorted(cues, key=lambda c: (c["start_ms"], c["end_ms"], c["cue_id"]))


def _issues_map(cues, shots, settings):
    issues = analyzer.analyze(cues, shots, settings)
    return {c["cue_id"]: iss for c, iss in zip(cues, issues)}


def _has_error(issues):
    return any(i["severity"] == "error" for i in issues)


def _current_state(conn) -> dict:
    proj = db.get_project(conn)
    cues = db.load_cues(conn)
    shots = db.load_shots(conn)
    solution = db.get_solution(conn)
    return {
        "subtitle": {
            "filename": proj["subtitle_filename"],
            "format": proj["subtitle_format"],
            "sha256": proj["subtitle_sha256"],
            "cue_count": len(cues),
        },
        "shots": {"filename": proj["shots_filename"], "shot_count": len(shots)},
        "settings": db.get_settings(conn),
        "has_solution": solution is not None,
        "export_count": proj["export_count"],
        "solution": solution,
    }


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------

@app.post("/api/import")
async def import_files(subtitle: UploadFile | None = File(None),
                       shots: UploadFile | None = File(None)):
    if subtitle is None and shots is None:
        raise HTTPException(400, "请至少上传一个字幕文件（SRT/VTT）或镜头 JSON 文件")

    with db.get_conn() as conn:
        if subtitle is not None:
            raw = await subtitle.read()
            try:
                text = raw.decode("utf-8-sig")
                parsed = parsers.parse_subtitle(text, subtitle.filename)
            except (ValueError, UnicodeDecodeError) as e:
                raise HTTPException(400, f"字幕解析失败：{e}")
            db.replace_cues(conn, parsed["cues"])
            conn.execute(
                "UPDATE project SET subtitle_format=?, subtitle_filename=?,"
                " subtitle_raw=?, subtitle_sha256=?, updated_at=datetime('now')"
                " WHERE id='default'",
                (parsed["format"], subtitle.filename, text,
                 hashlib.sha256(raw).hexdigest()),
            )
            db.save_solution(conn, None)

        if shots is not None:
            raw = await shots.read()
            try:
                text = raw.decode("utf-8-sig")
                shot_list = parsers.parse_shots(text)
            except (ValueError, UnicodeDecodeError) as e:  # JSONDecodeError 属 ValueError
                raise HTTPException(400, f"镜头 JSON 解析失败：{e}")
            db.replace_shots(conn, shot_list)
            conn.execute(
                "UPDATE project SET shots_filename=?, shots_raw=?,"
                " updated_at=datetime('now') WHERE id='default'",
                (shots.filename, text),
            )
            db.save_solution(conn, None)

        return _current_state(conn)


@app.post("/api/reset")
def reset_all():
    with db.get_conn() as conn:
        db.reset_imports(conn)
        conn.execute("UPDATE project SET export_count=0 WHERE id='default'")
        return _current_state(conn)


# ---------------------------------------------------------------------------
# 设置 / 状态 / 分析
# ---------------------------------------------------------------------------

@app.get("/api/state")
def get_state():
    with db.get_conn() as conn:
        return _current_state(conn)


@app.put("/api/settings")
def update_settings(body: SettingsIn):
    with db.get_conn() as conn:
        db.save_settings(conn, body.model_dump())
        db.save_solution(conn, None)  # 参数变化后旧方案失效
        return db.get_settings(conn)


@app.get("/api/analysis")
def get_analysis():
    with db.get_conn() as conn:
        cues = _ordered_cues(conn)
        if not cues:
            raise HTTPException(400, "尚未导入字幕")
        shots = db.load_shots(conn)
        settings = db.get_settings(conn)
        issues = analyzer.analyze(cues, shots, settings)
        return {
            "settings": settings,
            "shots": [
                {"shot_id": s["shot_id"], "start_ms": s["start_ms"], "end_ms": s["end_ms"]}
                for s in shots
            ],
            "cues": [
                {
                    "cue_id": c["cue_id"],
                    "order": i,
                    "start_ms": c["start_ms"],
                    "end_ms": c["end_ms"],
                    "duration_ms": c["end_ms"] - c["start_ms"],
                    "grapheme_count": c["grapheme_count"],
                    "text": c["text"],
                    "issues": issues[i],
                }
                for i, c in enumerate(cues)
            ],
        }


# ---------------------------------------------------------------------------
# 校正求解
# ---------------------------------------------------------------------------

@app.post("/api/solve")
def run_solve():
    with db.get_conn() as conn:
        cues = _ordered_cues(conn)
        if not cues:
            raise HTTPException(400, "尚未导入字幕")
        shots = db.load_shots(conn)
        settings = db.get_settings(conn)

        orig_issues = _issues_map(cues, shots, settings)
        error_ids = {cid for cid, iss in orig_issues.items() if _has_error(iss)}

        result = solver.solve(cues, shots, settings, original_error_ids=error_ids)

        # 校正后剩余问题（用于展示与导出）
        corrected_cues = []
        for it in result["items"]:
            corrected_cues.append({
                "cue_id": it["cue_id"],
                "start_ms": it["corrected_start_ms"],
                "end_ms": it["corrected_end_ms"],
                "text": it["text"],
                "grapheme_count": it["grapheme_count"],
            })
        after_issues = analyzer.analyze(
            sorted(corrected_cues, key=lambda c: (c["start_ms"], c["end_ms"], c["cue_id"])),
            shots, settings,
        )
        ordered_after = sorted(corrected_cues,
                               key=lambda c: (c["start_ms"], c["end_ms"], c["cue_id"]))
        after_map = {c["cue_id"]: iss for c, iss in zip(ordered_after, after_issues)}
        for it in result["items"]:
            it["original_issues"] = orig_issues.get(it["cue_id"], [])
            it["issues_after"] = after_map.get(it["cue_id"], [])

        db.save_solution(conn, result)
        return result


# ---------------------------------------------------------------------------
# 采纳
# ---------------------------------------------------------------------------

def _get_solution_or_404(conn) -> dict:
    sol = db.get_solution(conn)
    if sol is None:
        raise HTTPException(400, "尚无校正方案，请先执行校正")
    return sol


@app.post("/api/cues/{cue_id}/adopt")
def adopt_one(cue_id: str, body: AdoptIn):
    with db.get_conn() as conn:
        sol = _get_solution_or_404(conn)
        found = False
        for it in sol["items"]:
            if it["cue_id"] == cue_id:
                if it["status"] in ("conflict", "cps_conflict"):
                    raise HTTPException(400, f"{cue_id} 处于冲突状态，无法采纳")
                it["adopted"] = body.adopted
                found = True
                break
        if not found:
            raise HTTPException(404, f"未找到 cue：{cue_id}")
        db.save_solution(conn, sol)
        return {"cue_id": cue_id, "adopted": body.adopted}


class AdoptMany(BaseModel):
    cue_ids: list[str] | None = None  # None 表示全部可采纳项


@app.post("/api/adopt")
def adopt_many(body: AdoptMany | None = None):
    ids = body.cue_ids if body and body.cue_ids is not None else None
    with db.get_conn() as conn:
        sol = _get_solution_or_404(conn)
        wanted = set(ids) if ids is not None else None
        for it in sol["items"]:
            if it["status"] in ("conflict", "cps_conflict"):
                continue
            if wanted is None or it["cue_id"] in wanted:
                it["adopted"] = True
        db.save_solution(conn, sol)
        return {"adopted_count": sum(1 for it in sol["items"] if it["adopted"])}


@app.post("/api/adopt/reset")
def adopt_reset():
    with db.get_conn() as conn:
        sol = _get_solution_or_404(conn)
        for it in sol["items"]:
            it["adopted"] = False
        db.save_solution(conn, sol)
        return {"ok": True}


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

@app.post("/api/export")
def export_json():
    with db.get_conn() as conn:
        proj = db.get_project(conn)
        cues = _ordered_cues(conn)
        shots = db.load_shots(conn)
        settings = db.get_settings(conn)
        sol = db.get_solution(conn)
        if sol is None:
            raise HTTPException(400, "尚无校正方案，请先执行校正")

        orig_issues = _issues_map(cues, shots, settings)
        version = proj["export_count"] + 1
        conn.execute("UPDATE project SET export_count=? WHERE id='default'", (version,))

        cue_entries = []
        adopted_count = 0
        for it in sol["items"]:
            if it["adopted"]:
                adopted_count += 1
            notes = []
            if it["status"] == "cps_conflict":
                chain = next((c for c in sol["conflict_chains"]
                              if c["type"] == "cps_intrinsic" and it["cue_id"] in c["cue_ids"]), None)
                notes.append(chain["reason"] if chain else "CPS 超限，平移无法修复")
            if it["status"] == "conflict":
                chain = next((c for c in sol["conflict_chains"]
                              if it["cue_id"] in c.get("cue_ids", [])), None)
                if chain:
                    notes.append(f"冲突链[{chain['type']}]：{chain['reason']}"
                                 f"（涉及 {', '.join(chain['cue_ids'])}）")
            remaining = [i["message"] for i in it.get("issues_after", [])
                         if i["severity"] == "error"]
            cue_entries.append({
                "cue_id": it["cue_id"],
                "order": it["order"],
                "text": it["text"],
                "grapheme_count": it["grapheme_count"],
                "original": {
                    "start_ms": it["original_start_ms"],
                    "end_ms": it["original_end_ms"],
                },
                "duration_ms": it["duration_ms"],
                "adopted": it["adopted"],
                "correction": {
                    "start_ms": it["corrected_start_ms"],
                    "end_ms": it["corrected_end_ms"],
                    "delta_ms": it["delta_ms"],
                } if it["adopted"] else None,
                "proposed_correction": {
                    "start_ms": it["corrected_start_ms"],
                    "end_ms": it["corrected_end_ms"],
                    "delta_ms": it["delta_ms"],
                },
                "status": it["status"],
                "original_issues": [i["message"] for i in orig_issues.get(it["cue_id"], [])],
                "remaining_issues_after_proposal": remaining,
                "conflict_note": "；".join(notes) if notes else None,
            })

        payload = {
            "app": "subtitle-rhythm-shot-boundary-correction-desk",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "correction_version": version,
            "source": {
                "subtitle": {
                    "filename": proj["subtitle_filename"],
                    "format": proj["subtitle_format"],
                    "sha256": proj["subtitle_sha256"],
                    "cue_count": len(cues),
                },
                "shots": {
                    "filename": proj["shots_filename"],
                    "shot_count": len(shots),
                },
            },
            "settings": settings,
            "summary": {
                **sol["stats"],
                "adopted_count": adopted_count,
                "feasible": sol["feasible"],
            },
            "feasible": sol["feasible"],
            "conflict_chains": sol["conflict_chains"],
            "cues": cue_entries,
        }
        fname = f"correction-v{version}.json"
        return JSONResponse(
            payload,
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
