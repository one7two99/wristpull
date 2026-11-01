"""Goal wizard: multiple-choice questions -> the generated part of the goal profile."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

MARK_START = "<!-- wristpull:wizard:start"
MARK_END = "<!-- wristpull:wizard:end -->"
NOTES_HEADING = {"en": "## My notes", "de": "## Meine Notizen"}


@dataclass
class Question:
    key: str
    text: dict[str, str]  # lang -> question
    options: list[tuple[str, dict[str, str]]] = field(default_factory=list)  # (value, lang -> label); empty = free text
    multi: bool = False
    hint: dict[str, str] = field(default_factory=dict)  # for free text: example / placeholder hint


QUESTIONS: list[Question] = [
    Question("goals", {"en": "What are you training for?", "de": "Wofür trainierst du?"}, [
        ("endurance", {"en": "Endurance / running", "de": "Ausdauer / Laufen"}),
        ("fitness", {"en": "General fitness and health", "de": "Allgemeine Fitness und Gesundheit"}),
        ("strength", {"en": "Strength / body composition", "de": "Kraft / Körperzusammensetzung"}),
        ("event", {"en": "A concrete event", "de": "Ein konkretes Event"}),
    ], multi=True),
    Question("event", {"en": "Event, date and target (if any)", "de": "Event, Datum und Ziel (falls vorhanden)"},
             hint={"en": "e.g. 5000 m in 22 min on 2027-02-01", "de": "z. B. 5000 m in 22 min am 2027-02-01"}),
    Question("weekly_hours", {"en": "Hours per week for training", "de": "Stunden pro Woche fürs Training"}, [
        ("lt3", {"en": "under 3", "de": "unter 3"}), ("3to5", {"en": "3 to 5", "de": "3 bis 5"}),
        ("5to8", {"en": "5 to 8", "de": "5 bis 8"}), ("gt8", {"en": "more than 8", "de": "mehr als 8"}),
    ]),
    Question("sessions", {"en": "Sessions per week and fixed rest day", "de": "Einheiten pro Woche und fester Ruhetag"},
             hint={"en": "e.g. 4 sessions, rest on Saturday", "de": "z. B. 4 Einheiten, Ruhetag Samstag"}),
    Question("experience", {"en": "Training experience", "de": "Trainingserfahrung"}, [
        ("beginner", {"en": "beginner", "de": "Anfänger"}), ("intermediate", {"en": "intermediate", "de": "fortgeschritten"}),
        ("advanced", {"en": "advanced", "de": "erfahren"}),
    ]),
    Question("preferences", {"en": "Preferred ways to train", "de": "Bevorzugte Trainingsformen"}, [
        ("run_outdoor", {"en": "running outdoors", "de": "Laufen draußen"}), ("treadmill", {"en": "treadmill", "de": "Laufband"}),
        ("cycling", {"en": "cycling", "de": "Rad"}), ("strength_home", {"en": "strength at home", "de": "Kraft zu Hause"}),
        ("gym", {"en": "gym", "de": "Studio"}), ("swimming", {"en": "swimming", "de": "Schwimmen"}),
    ], multi=True),
    Question("constraints", {"en": "Injuries, health limits, other constraints", "de": "Verletzungen, gesundheitliche Grenzen, sonstige Einschränkungen"},
             hint={"en": "e.g. left knee sensitive on downhills; none", "de": "z. B. linkes Knie bergab empfindlich; keine"}),
    Question("style", {"en": "How should the coach plan?", "de": "Wie soll der Coach planen?"}, [
        ("cautious", {"en": "cautious", "de": "vorsichtig"}), ("balanced", {"en": "balanced", "de": "ausgewogen"}),
        ("ambitious", {"en": "ambitious", "de": "ambitioniert"}),
    ]),
    Question("tone", {"en": "Tone of the answers", "de": "Ton der Antworten"}, [
        ("terse", {"en": "terse", "de": "knapp"}), ("detailed", {"en": "detailed", "de": "ausführlich"}),
    ]),
    Question("details", {"en": "Optional: age, weight, max heart rate", "de": "Optional: Alter, Gewicht, maximale Herzfrequenz"},
             hint={"en": "e.g. 51 years, 70 kg, max HR 180", "de": "z. B. 51 Jahre, 70 kg, max HF 180"}),
]

FRAGMENTS: dict[str, dict[str, dict[str, str]]] = {
    "goals": {
        "endurance": {"en": "improve endurance and running performance", "de": "Ausdauer und Laufleistung verbessern"},
        "fitness": {"en": "general fitness and health (regularity, sleep, recovery, stress)", "de": "allgemeine Fitness und Gesundheit (Regelmäßigkeit, Schlaf, Erholung, Stress)"},
        "strength": {"en": "strength and body composition", "de": "Kraft und Körperzusammensetzung"},
        "event": {"en": "prepare for a concrete event", "de": "ein konkretes Event vorbereiten"},
    },
    "weekly_hours": {
        "lt3": {"en": "under 3 hours per week", "de": "unter 3 Stunden pro Woche"},
        "3to5": {"en": "3 to 5 hours per week", "de": "3 bis 5 Stunden pro Woche"},
        "5to8": {"en": "5 to 8 hours per week", "de": "5 bis 8 Stunden pro Woche"},
        "gt8": {"en": "more than 8 hours per week", "de": "mehr als 8 Stunden pro Woche"},
    },
    "experience": {
        "beginner": {"en": "Beginner: build the habit first, progress slowly, explain the why.", "de": "Anfänger: erst die Gewohnheit aufbauen, langsam steigern, das Warum erklären."},
        "intermediate": {"en": "Intermediate: knows the basics, can handle structured sessions and zone work.", "de": "Fortgeschritten: kennt die Grundlagen, kommt mit strukturierten Einheiten und Zonen zurecht."},
        "advanced": {"en": "Advanced: years of training, wants precise and demanding sessions.", "de": "Erfahren: jahrelang trainiert, will präzise und fordernde Einheiten."},
    },
    "style": {
        "cautious": {"en": "Plan cautiously: when in doubt choose the easier option, add volume in small steps, favour recovery.", "de": "Vorsichtig planen: im Zweifel die leichtere Variante, Umfang in kleinen Schritten, Erholung bevorzugen."},
        "balanced": {"en": "Plan balanced: steady progression, one harder session per week when recovery allows.", "de": "Ausgewogen planen: stetige Steigerung, eine härtere Einheit pro Woche, wenn die Erholung es erlaubt."},
        "ambitious": {"en": "Plan ambitiously: use the available time fully, two quality sessions per week, but respect the warning flags.", "de": "Ambitioniert planen: die Zeit voll nutzen, zwei Qualitätseinheiten pro Woche, Warnflags aber respektieren."},
    },
    "tone": {
        "terse": {"en": "Answer tersely, in short sentences and bullet points.", "de": "Knapp antworten, kurze Sätze und Stichpunkte."},
        "detailed": {"en": "Answer in detail and explain the reasoning behind each suggestion.", "de": "Ausführlich antworten und die Begründung jeder Empfehlung erklären."},
    },
}
PREFERENCE_LABELS = {opt[0]: opt[1] for q in QUESTIONS if q.key == "preferences" for opt in q.options}

HEADINGS = {
    "en": {"title": "# Goal profile", "goals": "Goals", "event": "Event", "time": "Time budget", "experience": "Experience", "preferences": "Preferences", "constraints": "Constraints", "style": "Coaching style", "details": "Athlete"},
    "de": {"title": "# Zielprofil", "goals": "Ziele", "event": "Event", "time": "Zeitbudget", "experience": "Erfahrung", "preferences": "Vorlieben", "constraints": "Einschränkungen", "style": "Coaching-Stil", "details": "Athlet"},
}


def _text(answers: dict[str, Any], key: str, placeholder: str) -> str:
    value = str(answers.get(key) or "").strip()
    return value if value else "{{" + placeholder + "}}"


def _list(answers: dict[str, Any], key: str) -> list[str]:
    value = answers.get(key) or []
    return [value] if isinstance(value, str) else [str(v) for v in value]


def generate_goal_prompt(answers: dict[str, Any], lang: str) -> str:
    """The wizard-generated block (between markers) from the answers; unanswered fields become placeholders."""
    lang = lang if lang in ("en", "de") else "en"
    h = HEADINGS[lang]
    goals = [FRAGMENTS["goals"][g][lang] for g in _list(answers, "goals") if g in FRAGMENTS["goals"]]
    lines = [
        f"- {h['goals']}: " + ("; ".join(goals) if goals else "{{goals}}"),
        f"- {h['event']}: " + (_text(answers, "event", "event") if ("event" in _list(answers, "goals") or answers.get("event")) else ("none" if lang == "en" else "keins")),
        f"- {h['time']}: " + (FRAGMENTS["weekly_hours"].get(str(answers.get("weekly_hours") or ""), {}).get(lang) or "{{weekly_hours}}") + "; " + _text(answers, "sessions", "sessions_and_rest_day"),
        f"- {h['experience']}: " + (FRAGMENTS["experience"].get(str(answers.get("experience") or ""), {}).get(lang) or "{{experience}}"),
        f"- {h['preferences']}: " + (", ".join(PREFERENCE_LABELS[p][lang] for p in _list(answers, "preferences") if p in PREFERENCE_LABELS) or "{{preferences}}"),
        f"- {h['constraints']}: " + _text(answers, "constraints", "constraints"),
        f"- {h['style']}: " + (FRAGMENTS["style"].get(str(answers.get("style") or ""), {}).get(lang) or "{{style}}") + " " + (FRAGMENTS["tone"].get(str(answers.get("tone") or ""), {}).get(lang) or ""),
        f"- {h['details']}: " + (str(answers.get("details")).strip() if answers.get("details") else ("not given" if lang == "en" else "keine Angabe")),
    ]
    body = "\n".join(lines).rstrip()
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    return f"{MARK_START} sha256={digest} -->\n{h['title']}\n\n{body}\n{MARK_END}"


def merge_profile(existing: str | None, generated: str, lang: str) -> str:
    """Replace the marker block of an existing profile (or prepend it) and keep everything else, e.g. the notes."""
    notes_heading = NOTES_HEADING.get(lang, NOTES_HEADING["en"])
    if existing and MARK_START in existing and MARK_END in existing:
        pattern = re.compile(re.escape(MARK_START) + r".*?" + re.escape(MARK_END), re.S)
        return pattern.sub(lambda _m: generated, existing, count=1)
    rest = (existing or "").strip()
    if not rest:
        rest = f"{notes_heading}\n\n" + ("(anything else the coach should know; this part survives a re-run of the wizard)" if lang == "en" else "(alles Weitere, was der Coach wissen soll; dieser Teil bleibt bei einem erneuten Assistentenlauf erhalten)")
    return f"{generated}\n\n{rest}\n"


def answers_to_toml(answers: dict[str, Any]) -> str:
    lines = ["# answers of the goal wizard; re-run `wristpull coach profile --wizard` to change them"]
    for q in QUESTIONS:
        value = answers.get(q.key)
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, list):
            lines.append(f"{q.key} = [" + ", ".join(f'"{v}"' for v in value) + "]")
        else:
            lines.append(f'{q.key} = "{str(value).replace(chr(34), chr(39))}"')
    return "\n".join(lines) + "\n"
