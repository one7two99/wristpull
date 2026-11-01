"""Persist coach runs and feedback; build the digest of earlier advice for the next prompt."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from .prompts import parse_sections

RUN_COLUMNS = (
    "kind", "period_start", "period_end", "language", "backend", "model", "profile_sha256", "prompt_sha256",
    "system_prompt", "user_prompt", "question", "answer_md", "input_tokens", "output_tokens", "tokens_estimated",
    "cost_usd", "duration_s", "stop_reason", "truncated", "status", "error",
)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_run(conn: sqlite3.Connection, **fields: Any) -> int:
    unknown = set(fields) - set(RUN_COLUMNS)
    if unknown:
        raise ValueError(f"unknown coach_runs columns: {sorted(unknown)}")
    row = {col: fields.get(col) for col in RUN_COLUMNS}
    row["tokens_estimated"] = int(bool(row.get("tokens_estimated")))
    row["truncated"] = int(bool(row.get("truncated")))
    row["status"] = row.get("status") or "ok"
    row["created_at_utc"] = _now()
    columns = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    cur = conn.execute(f"INSERT INTO coach_runs ({columns}) VALUES ({placeholders})", tuple(row.values()))
    conn.commit()
    return int(cur.lastrowid)


def get_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM coach_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def recent_runs(conn: sqlite3.Connection, *, kind: str | None = None, limit: int = 10, status: str = "ok") -> list[dict[str, Any]]:
    sql = "SELECT r.*, (SELECT rating FROM coach_feedback f WHERE f.run_id = r.id ORDER BY f.id DESC LIMIT 1) AS rating FROM coach_runs r WHERE status = ?"
    params: list[Any] = [status]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def add_feedback(conn: sqlite3.Connection, run_id: int, rating: int, note: str | None = None) -> int:
    if rating not in (-1, 0, 1):
        raise ValueError("rating must be -1, 0 or 1")
    if get_run(conn, run_id) is None:
        raise ValueError(f"no coach run {run_id}")
    cur = conn.execute("INSERT INTO coach_feedback (run_id, rating, note, created_at_utc) VALUES (?, ?, ?, ?)", (run_id, rating, note, _now()))
    conn.commit()
    return int(cur.lastrowid)


def memory_digest(conn: sqlite3.Connection, lang: str, *, daily: int = 3, weekly: int = 2, max_chars: int = 1800) -> str:
    """Compact Markdown of earlier advice (suggestion / plan sections) with the user's rating, newest first."""
    lines: list[str] = []
    picked = recent_runs(conn, kind="daily", limit=daily) + recent_runs(conn, kind="weekly", limit=weekly)
    for run in sorted(picked, key=lambda r: r["id"], reverse=True):
        sections = parse_sections(run.get("answer_md") or "", run["kind"], run.get("language") or lang)
        key = "plan" if run["kind"] == "weekly" else "suggestion"
        text = sections.get(key) or (run.get("answer_md") or "").strip()
        text = " ".join(text.split())
        if len(text) > 400:
            text = text[:397] + "…"
        rating = run.get("rating")
        mark = {1: "👍", 0: "😐", -1: "👎"}.get(rating, "–") if rating is not None else "–"
        lines.append(f"- {run['created_at_utc'][:16].replace('T', ' ')} UTC {run['kind']} (feedback {mark}): {text}")
    out: list[str] = []
    total = 0
    for line in lines:
        if total + len(line) > max_chars:
            break
        out.append(line)
        total += len(line)
    return "\n".join(out)
