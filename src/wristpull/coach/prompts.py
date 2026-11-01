"""Prompt text: system prompt, user prompt layout, the output contract and its parser."""

from __future__ import annotations

import re

LANGUAGE_NAMES = {"en": "English", "de": "German"}

# Fixed H2 headings the model must use, per kind and language (key -> heading).
SECTIONS: dict[str, dict[str, list[tuple[str, str]]]] = {
    "daily": {
        "en": [("status", "Status"), ("suggestion", "Today's suggestion"), ("watch", "Watch out for"), ("numbers", "Numbers used")],
        "de": [("status", "Status"), ("suggestion", "Vorschlag für heute"), ("watch", "Worauf achten"), ("numbers", "Verwendete Zahlen")],
    },
    "weekly": {
        "en": [("review", "Week in review"), ("recovery", "Recovery"), ("load", "Training load"), ("plan", "Plan for next week"), ("questions", "Questions for you"), ("numbers", "Numbers used")],
        "de": [("review", "Rückblick"), ("recovery", "Erholung"), ("load", "Belastung"), ("plan", "Plan nächste Woche"), ("questions", "Fragen an dich"), ("numbers", "Verwendete Zahlen")],
    },
    "ask": {
        "en": [("answer", "Answer"), ("numbers", "Numbers used")],
        "de": [("answer", "Antwort"), ("numbers", "Verwendete Zahlen")],
    },
}

TASKS = {
    "daily": (
        "Give the morning check for today: how recovered the athlete is, one concrete suggestion for today "
        "(session type, duration, intensity, or rest) and at most two things to watch out for. "
        "Keep the suggestion to about 150 words."
    ),
    "weekly": (
        "Review the past week against the previous one, assess recovery and training load, and propose a plan "
        "for the next week as a table with one row per day (day, session, duration, intensity). "
        "Finish with up to three questions whose answers would improve the next plan."
    ),
    "ask": "Answer the athlete's question using only the data provided.",
}

DEFAULT_PROFILE = {
    "en": """# Goal profile

Fill the placeholders in double braces and edit freely; this text is sent to the coach with every run.

- Primary goals: {{goals}} (e.g. improve endurance running; general fitness and health)
- Event: {{event}} (e.g. half marathon on {{event_date}}, target {{target_time}}; or "none")
- Time budget: {{weekly_hours}} hours per week, {{sessions_per_week}} sessions, fixed rest day {{rest_day}}
- Experience: {{experience}} (beginner / intermediate / advanced)
- Preferences: {{preferences}} (outdoor running, treadmill, cycling, strength at home, ...)
- Constraints and injuries: {{constraints}}
- Optional: age {{age}}, weight {{weight_kg}} kg, max HR {{max_hr}}
- Style: {{style}} (cautious / balanced / ambitious); tone: {{tone}} (terse / detailed)
""",
    "de": """# Zielprofil

Fülle die Platzhalter in doppelten Klammern aus und bearbeite den Text frei; er geht bei jedem Lauf an den Coach.

- Hauptziele: {{goals}} (z. B. Ausdauer im Laufen verbessern; allgemeine Fitness und Gesundheit)
- Event: {{event}} (z. B. Halbmarathon am {{event_date}}, Zielzeit {{target_time}}; oder „keins“)
- Zeitbudget: {{weekly_hours}} Stunden pro Woche, {{sessions_per_week}} Einheiten, fester Ruhetag {{rest_day}}
- Erfahrung: {{experience}} (Anfänger / Fortgeschritten / Erfahren)
- Vorlieben: {{preferences}} (Laufen draußen, Laufband, Rad, Kraft zu Hause, ...)
- Einschränkungen und Verletzungen: {{constraints}}
- Optional: Alter {{age}}, Gewicht {{weight_kg}} kg, maximale HF {{max_hr}}
- Stil: {{style}} (vorsichtig / ausgewogen / ambitioniert); Ton: {{tone}} (knapp / ausführlich)
""",
}

PLACEHOLDER = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")


def unfilled_placeholders(text: str) -> list[str]:
    return sorted({m.group(1) for m in PLACEHOLDER.finditer(text)})


def system_prompt(kind: str, lang: str) -> str:
    headings = "\n".join(f"## {heading}" for _, heading in SECTIONS[kind][lang])
    return (
        "You are a sober, experienced endurance and fitness coach working with data from the athlete's own "
        "wearables (a Garmin watch and a heart-rate strap), aggregated per day and per week.\n\n"
        "Rules:\n"
        "- Use only the numbers in the data package. Never invent, extrapolate or round beyond what is given. "
        "Every number you mention must appear in the data and must be repeated in the last section.\n"
        "- Missing values are missing, not zero. Say so when a judgement rests on thin data.\n"
        "- When the package lists warning flags, be conservative: reduce volume or intensity, or propose rest.\n"
        "- The goal profile's constraints (injuries, time budget, rest day) are hard limits.\n"
        "- No medical diagnoses or medication advice. If values look pathological (resting HR far above normal, "
        "SpO2 below 90 %, chest pain mentioned), recommend seeing a doctor instead of training advice.\n"
        "- Be concrete: durations in minutes, intensities as easy/moderate/hard or heart-rate zones.\n\n"
        f"Answer in {LANGUAGE_NAMES.get(lang, 'English')}. Use exactly these Markdown H2 headings, in this order, "
        f"with nothing before the first one:\n{headings}\n"
    )


def user_prompt(kind: str, *, profile_md: str, data_md: str, digest_md: str, question: str | None, lang: str) -> str:
    headings = "\n".join(f"## {heading}" for _, heading in SECTIONS[kind][lang])
    parts = [
        "# Goal profile\n\n" + profile_md.strip(),
        "# Data\n\n" + data_md.strip(),
        "# Previous coaching\n\n" + (digest_md.strip() or "(none yet)"),
        "# Task\n\n" + TASKS[kind] + (f"\n\nQuestion: {question.strip()}" if question else ""),
        "# Output format\n\nUse exactly these H2 headings in this order:\n" + headings,
    ]
    return "\n\n".join(parts) + "\n"


def parse_sections(answer: str, kind: str, lang: str) -> dict[str, str]:
    """Split the answer by the contract headings (tolerant: any H2/H3 level, case-insensitive, either language).

    Text before the first known heading, or an answer without headings, lands in the first section key.
    """
    known: dict[str, str] = {}
    for language in SECTIONS[kind]:
        for key, heading in SECTIONS[kind][language]:
            known[heading.lower()] = key
    first_key = SECTIONS[kind][lang][0][0]
    sections: dict[str, list[str]] = {}
    current = first_key
    for line in answer.splitlines():
        match = re.match(r"^\s{0,3}#{2,3}\s*(.+?)\s*#*\s*$", line)
        if match:
            title = re.sub(r"[*_`]", "", match.group(1)).strip().lower().rstrip(":")
            key = known.get(title)
            if key is None:
                for heading, candidate in known.items():
                    if title.startswith(heading) or heading.startswith(title):
                        key = candidate
                        break
            if key is not None:
                current = key
                sections.setdefault(current, [])
                continue
        sections.setdefault(current, []).append(line)
    return {key: "\n".join(lines).strip() for key, lines in sections.items() if "\n".join(lines).strip()}


NUMBER = re.compile(r"(?<![\w.])-?\d+(?:[.,]\d+)?(?![\w.])")


def _normal(token: str) -> str:
    return token.replace(",", ".").lstrip("-").rstrip("0").rstrip(".") if "." in token.replace(",", ".") else token.lstrip("-")


def verify_numbers(answer: str, data_md: str, *, section: str | None = None) -> list[str]:
    """Numbers the answer mentions (its 'numbers used' section when given) that do not occur in the data package.

    Comparison is textual after normalising decimal commas and trailing zeros; small integers up to 10
    are ignored because they usually count things (zones, list items) rather than quote data.
    """
    haystack = {_normal(m.group(0)) for m in NUMBER.finditer(data_md)}
    # also accept the integer part of decimals in the data ("7.4 h" quoted as "7 h")
    haystack |= {h.split(".")[0] for h in haystack}
    missing: list[str] = []
    for m in NUMBER.finditer(section if section is not None else answer):
        raw = m.group(0)
        norm = _normal(raw)
        try:
            if abs(float(norm)) <= 10 and "." not in norm:
                continue
        except ValueError:
            continue
        if norm not in haystack and norm not in missing:
            missing.append(norm)
    return missing
