"""SQLite 持久化层（单项目，id 固定为 default）。"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get(
    "APP_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "app.db"),
)

DEFAULT_SETTINGS = {
    "max_cps": 20.0,
    "min_gap_ms": 40,
    "boundary_tolerance_ms": 120,
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = _connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS project (
                id TEXT PRIMARY KEY,
                subtitle_format TEXT,
                subtitle_filename TEXT,
                subtitle_raw TEXT,
                subtitle_sha256 TEXT,
                shots_filename TEXT,
                shots_raw TEXT,
                settings_json TEXT NOT NULL,
                solution_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS cue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cue_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL,
                text TEXT NOT NULL,
                grapheme_count INTEGER NOT NULL,
                UNIQUE(cue_id)
            );
            CREATE TABLE IF NOT EXISTS shot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shot_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL
            );
            """
        )
        row = conn.execute("SELECT id FROM project WHERE id='default'").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO project (id, settings_json) VALUES ('default', ?)",
                (json.dumps(DEFAULT_SETTINGS),),
            )
        # 轻量迁移：export_count
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(project)").fetchall()}
        if "export_count" not in cols:
            conn.execute("ALTER TABLE project ADD COLUMN export_count INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------

def get_project(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT * FROM project WHERE id='default'").fetchone()
    return dict(row) if row else None


def get_settings(conn: sqlite3.Connection) -> dict:
    return json.loads(get_project(conn)["settings_json"])


def save_settings(conn: sqlite3.Connection, settings: dict) -> None:
    conn.execute(
        "UPDATE project SET settings_json=?, updated_at=datetime('now') WHERE id='default'",
        (json.dumps(settings),),
    )


def save_solution(conn: sqlite3.Connection, solution: dict | None) -> None:
    conn.execute(
        "UPDATE project SET solution_json=?, updated_at=datetime('now') WHERE id='default'",
        (json.dumps(solution, ensure_ascii=False) if solution is not None else None,),
    )


def get_solution(conn: sqlite3.Connection) -> dict | None:
    raw = get_project(conn)["solution_json"]
    return json.loads(raw) if raw else None


def replace_cues(conn: sqlite3.Connection, cues: list[dict]) -> None:
    conn.execute("DELETE FROM cue")
    conn.executemany(
        "INSERT INTO cue (cue_id, seq, start_ms, end_ms, text, grapheme_count)"
        " VALUES (?,?,?,?,?,?)",
        [(c["cue_id"], i, c["start_ms"], c["end_ms"], c["text"], c["grapheme_count"])
         for i, c in enumerate(cues)],
    )


def replace_shots(conn: sqlite3.Connection, shots: list[dict]) -> None:
    conn.execute("DELETE FROM shot")
    conn.executemany(
        "INSERT INTO shot (shot_id, seq, start_ms, end_ms) VALUES (?,?,?,?)",
        [(s["shot_id"], i, s["start_ms"], s["end_ms"])
         for i, s in enumerate(shots)],
    )


def load_cues(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM cue ORDER BY seq").fetchall()
    return [dict(r) for r in rows]


def load_shots(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM shot ORDER BY seq").fetchall()
    return [dict(r) for r in rows]


def reset_imports(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM cue")
    conn.execute("DELETE FROM shot")
    conn.execute(
        "UPDATE project SET subtitle_format=NULL, subtitle_filename=NULL, subtitle_raw=NULL,"
        " subtitle_sha256=NULL, shots_filename=NULL, shots_raw=NULL, solution_json=NULL,"
        " updated_at=datetime('now') WHERE id='default'"
    )
