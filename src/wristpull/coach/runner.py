"""Orchestrate one coach run: profile + data package -> prompts -> backend -> stored result."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ..config import Config
from .backends import Backend, ChatRequest, CoachError, make_backend
from .context import build_daily, build_weekly, render
from .profile import coach_dir, load_profile
from .prompts import parse_sections, system_prompt, user_prompt, verify_numbers
from .store import memory_digest, save_run

Log = Callable[[str], None]
KINDS = ("daily", "weekly", "ask")


@dataclass
class CoachResult:
    run_id: int
    kind: str
    status: str
    answer_md: str = ""
    sections: dict[str, str] = field(default_factory=dict)
    backend: str = ""
    model: str = ""
    language: str = "en"
    input_tokens: int | None = None
    output_tokens: int | None = None
    tokens_estimated: bool = False
    cost_usd: float | None = None
    duration_s: float = 0.0
    dry_run_path: Path | None = None
    warnings: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def cost_estimate(config: Config, model: str, input_tokens: int | None, output_tokens: int | None) -> float | None:
    price = config.coach.price_for(model)
    if price is None or input_tokens is None or output_tokens is None:
        return None
    return round(input_tokens / 1e6 * price[0] + output_tokens / 1e6 * price[1], 5)


def resolve_language(config: Config, override: str | None) -> str:
    lang = override or config.coach.language or config.ui_language or "en"
    return lang if lang in ("en", "de") else "en"


def run_coach(
    conn: sqlite3.Connection,
    config: Config,
    kind: str,
    *,
    today: date,
    language: str | None = None,
    question: str | None = None,
    backend: Backend | None = None,
    dry_run: bool = False,
    force: bool = False,
    log: Log = lambda _m: None,
) -> CoachResult:
    if kind not in KINDS:
        raise CoachError(f"unknown coach run kind {kind!r}; available: {', '.join(KINDS)}")
    if kind == "ask" and not (question or "").strip():
        raise CoachError("a question is required for 'ask'")
    lang = resolve_language(config, language)
    profile_md, profile_sha, placeholders = load_profile(config, lang)
    warnings: list[str] = []
    if placeholders:
        message = f"goal profile still has unfilled placeholders: {', '.join(placeholders)} (edit {coach_dir(config) / 'profile.md'})"
        if not force and not dry_run:
            raise CoachError(message + "; pass --force to run anyway")
        warnings.append(message)
    digest = memory_digest(conn, lang)
    pkg = build_weekly(conn, config, today, digest=digest) if kind == "weekly" else build_daily(conn, config, today, digest=digest)
    data_md, dropped = render(pkg, budget_tokens=config.coach.token_budget)
    if dropped:
        warnings.append("sections omitted for the token budget: " + ", ".join(dropped))
    system = system_prompt(kind, lang)
    user = user_prompt(kind, profile_md=profile_md, data_md=data_md, digest_md="", question=question, lang=lang)
    prompt_sha = hashlib.sha256((system + user).encode("utf-8")).hexdigest()
    common: dict[str, Any] = dict(
        kind=kind, period_start=pkg.start.isoformat(), period_end=pkg.end.isoformat(), language=lang,
        profile_sha256=profile_sha, prompt_sha256=prompt_sha, system_prompt=system, user_prompt=user, question=question,
    )
    chosen = backend or make_backend(config, kind=kind)
    model = chosen.model

    if dry_run:
        out_dir = coach_dir(config) / "dryrun"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{kind}.md"
        path.write_text(f"# System prompt\n\n{system}\n\n# User prompt\n\n{user}", "utf-8")
        run_id = save_run(conn, backend=chosen.name, model=model, status="dry_run", **common)
        log(f"  dry run: prompt written to {path} (~{len(system + user) // 4} tokens)")
        return CoachResult(run_id, kind, "dry_run", backend=chosen.name, model=model, language=lang, dry_run_path=path, warnings=warnings, flags=pkg.flags)

    request = ChatRequest(system=system, user=user, model=model, max_tokens=config.coach.max_tokens)
    log(f"  sending ~{len(system + user) // 4} tokens to {chosen.name}/{model} …")
    try:
        response = chosen.complete(request, log=log)
    except CoachError as exc:
        save_run(conn, backend=chosen.name, model=model, status="error", error=str(exc), **common)
        raise
    sections = parse_sections(response.text, kind, lang)
    if response.truncated:
        warnings.append("answer was cut off at max_tokens")
    unknown = verify_numbers(response.text, data_md + "\n" + profile_md, section=sections.get("numbers"))
    if unknown:
        warnings.append("numbers not found in the data package: " + ", ".join(unknown[:12]) + (" …" if len(unknown) > 12 else ""))
    cost = response.cost_usd if response.cost_usd is not None else cost_estimate(config, response.model, response.input_tokens, response.output_tokens)
    run_id = save_run(
        conn,
        backend=response.backend, model=response.model, answer_md=response.text,
        input_tokens=response.input_tokens, output_tokens=response.output_tokens, tokens_estimated=response.tokens_estimated,
        cost_usd=cost, duration_s=round(response.duration_s, 1), stop_reason=response.stop_reason, truncated=response.truncated, **common,
    )
    log(f"  answer in {response.duration_s:.0f} s, {response.input_tokens or '?'} in / {response.output_tokens or '?'} out tokens")
    return CoachResult(
        run_id, kind, "ok", answer_md=response.text, sections=sections, backend=response.backend, model=response.model, language=lang,
        input_tokens=response.input_tokens, output_tokens=response.output_tokens, tokens_estimated=response.tokens_estimated,
        cost_usd=cost, duration_s=response.duration_s, warnings=warnings, flags=pkg.flags,
    )
