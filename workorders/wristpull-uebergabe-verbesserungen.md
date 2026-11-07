# Übergabe: wristpull – Verbesserungen und neue Features (Punkte 1–30)

Stand: 2026-11-07, Repository-Stand Commit `34e26a7` (2026-11-03 22:49, „Docs: web dashboard in README …“).
Zweck: Fortsetzung der Review- und Planungsarbeit in einem neuen Claude-Chat. Dieses Dokument
enthält den Projektkontext, die abgestimmte Liste der 30 Vorschläge mit Status und Bedeutung sowie die
empfohlene Reihenfolge.

---

## 1. Projekt in Kürze

- **wristpull** sammelt Gesundheits- und Trainingsdaten von Wearables **ohne Hersteller-Cloud**. Es
  archiviert die Rohdateien unveränderlich (SHA-256), normalisiert alles in eine lokale SQLite-Datenbank
  und bereitet die Daten für die Analyse mit einem LLM auf.
- Repository: <https://github.com/one7two99/wristpull> (öffentlich, MIT), Branch `master`
- Projektseite: <https://one7two99.github.io/wristpull/> (`docs/index.html`, GitHub Pages aus `/docs`)
- Ziel: wristpull auf einer eigenen Website veröffentlichen („Launch“).
- Geräte im täglichen Einsatz: Garmin fenix 6X Pro (USB/MTP und Gadgetbridge per ADB) und Amazfit
  Helio Strap (Gadgetbridge-Datenbankexport per ADB).
- Stack: Python 3.14, uv, argparse-CLI, stdlib `sqlite3` (WAL), `fitdecode`, polars. Optionale Extras:
  `tui` (Textual), `coach` (anthropic SDK), `web` (Starlette, uvicorn, Jinja2).
- Umfang: ~190 Dateien, 224 Tests grün (1 übersprungen), pyflakes sauber, Version `0.2.0`, **keine Git-Tags**.

### Was bereits existiert

| Bereich | Inhalt |
|---|---|
| Import | `import garmin` (USB/MTP über gio/gvfs, KDE-kiod6-Workaround), `pull phone` (ADB, Gadgetbridge-Intents, Hash-Abgleich auf dem Handy), `import gadgetbridge`, `reparse` |
| Ausgabe | `export --for-claude` (Markdown + CSV), `report week\|month` (mit SVG-Charts) |
| TUI | 10 Screens (Tasten 1–9, 0), Deutsch/Englisch, Jobs im Worker-Thread |
| Coach | `coach daily/weekly/ask`, Backends Ollama/Claude/OpenRouter, Kennzahlen deterministisch in Python, Zahlenprüfung gegen Halluzinationen, Bewertungen, Journal, Plan-Adherence, ICS-Export, Ziel-Wizard |
| Workouts | Markdown-Workouts → FIT → `GARMIN/NewFiles/` der Uhr, Intervallblöcke, `verify`, `watch`, `remove`, `from-plan`; Workout-Limit der fenix (20–25) berücksichtigt |
| Web-Dashboard (neu, D1–D3 am 2026-11-03) | `wristpull serve`, Seiten Today/Trends/Activities/Sleep/Coach/Workouts, Coach-Läufe als Hintergrundjobs, Bewertung, Journal, Workout-Editor mit Live-Check; Docker + Caddy (`tls internal`, Port 8443); Login mit scrypt-Hash, signiertem Cookie, CSRF, Rate-Limit, strenger CSP; genau ein uvicorn-Worker; htmx/ECharts lokal eingebettet; Boundary-Test (kein SQL in `web/`) |
| Automatik | `contrib/systemd/` (Pull 06:45, Coach 07:00, Import-Service), `contrib/udev/` (Import beim Einstecken) |
| Konzepte | `docs/coach-concept.md`, `docs/tui-concept.md`, `docs/dashboard-concept.md` (inkl. geplantem Server-Umzug) |

Das Web-Dashboard ist sorgfältig gebaut. Geprüft wurden: Path-Traversal-Schutz im Workout-Editor
(`workouts/store.workout_file`), `<`-Escaping der Chart-JSON-Daten, Cookie `HttpOnly/Secure/SameSite=Strict`,
CSP ohne `unsafe-inline` für Skripte.

---

## 2. Arbeitsweise und Rahmenbedingungen

- **Philipp setzt Änderungen nicht manuell um.** Umsetzungen werden als **Work-Order-Dokument für Claude
  Code** geliefert, nicht als Schritt-für-Schritt-Anleitung.
- Gespräch auf **Deutsch**. Code, Kommentare, Commits und README auf **Englisch** (Regel aus `CLAUDE.md`).
- Nicht am Ende jeder Antwort fragen, ob es für heute reicht.
- Repo-Regeln aus `CLAUDE.md`, die jede Work-Order übernehmen muss:
  - Gesundheitsdaten kommen nie ins Repo. Tests nutzen nur synthetische FIT-Dateien (`wristpull.fit.writer`) und Fakes (`FakeAdb`, `FakeBackend`), nie Hardware oder Netzwerk.
  - Jede neue Funktion bekommt einen Test. Vor jedem Commit: `uv run pyflakes src tests && uv run pytest -q`.
  - Rohdateien sind unveränderlich. Auf der Uhr wird nur gelesen; die einzigen Schreibausnahmen sind Workouts in `NewFiles/`/`Workouts/` und das Entfernen nach Backup.
  - Zeitstempel in UTC ISO-8601 mit `Z`, dazu `tz` und `local_date`.
  - TUI und Web rufen nur Bibliotheksfunktionen auf, ohne SQL und ohne FIT-Parsing (Boundary-Tests).
  - Strings in TUI und Web laufen über `_()`. Danach den deutschen Katalog aktualisieren und `.mo` neu kompilieren.
  - Nach jeder Welle: README aktualisieren, Tests grün, Commit.

### Abgrenzung zur Docker-Session

Der **Umbau auf Docker** (alle Funktionen inklusive Import und TUI im Container) läuft in einer
**eigenen Session** mit eigenem Übergabedokument (`wristpull-uebergabe-docker.md`). Davon betroffen:

- **Punkt 7 (Container-Image auf GHCR) wartet auf den Docker-Umbau.** Erst wenn `web` und `cli` aus einem Image laufen, lohnt das Veröffentlichen.
- **Punkt 8 (`doctor`), Punkt 10 (Linux-Hinweis) und Punkt 28 (`ship`)** müssen die Container-Variante mitdenken. Die Container-Variante ändert nichts daran, dass das Tool Linux-only ist, weil USB-Durchreichung unter Docker Desktop auf macOS und Windows nicht funktioniert.
- Konflikte vermeiden: Punkte, die `garmin_usb.py`, `workouts/push.py`, `workouts/watch.py`, `adb.py`, `compose.yaml`, `Dockerfile` oder `contrib/` anfassen, erst nach dem Docker-Umbau angehen oder mit ihm abstimmen.

---

## 3. Die 30 Punkte

Status: **offen**, **teilweise** (es gibt Vorarbeit) oder **erledigt**. Die Reihenfolge innerhalb der Gruppen entspricht grob der Priorität.

### A. Sicherheit und Betrieb

**1. Backup-Befehl `wristpull backup`** · offen · *höchste Priorität*
Coach-Läufe (`coach_runs`), Bewertungen (`coach_feedback`), Journal (`journal`), Push-Historie
(`workout_pushes`) und die im Browser bearbeiteten Workout-Dateien existieren **nur** in der Datenbank
bzw. im Datenordner. Ein `reparse` aus den Rohdaten stellt sie nicht wieder her. Vor dem Dashboard war die
Datenbank vollständig ableitbar, jetzt nicht mehr.
Gemeint ist: ein konsistenter Snapshot per `VACUUM INTO` (funktioniert im laufenden WAL-Betrieb,
während CLI und Container schreiben), dazu `workouts/` und `coach/profile.md`. Optional verschlüsselt
(z. B. mit age) und mit Rotation (z. B. 7 täglich, 4 wöchentlich). Dazu ein systemd-Timer-Template in
`contrib/` und ein Restore-Pfad, der getestet wird.

**2. Caddy-Root-Zertifikat aus dem Repo entfernen** · offen · *klein, sofort*
`wristpull-root.crt` (CN „Caddy Local Authority – 2026 ECC Root“) kam mit Commit `e391942` (D2)
ins Repo. Es enthält **keinen privaten Schlüssel**, ist also kein Sicherheitsleck. Es ist aber das persönliche
Zertifikat von Philipps Caddy-Instanz und für andere nutzlos.
Gemeint ist: Datei löschen und `*.crt`/`*.pem` in die `.gitignore` aufnehmen. Die Git-Historie muss nicht
umgeschrieben werden.

**3. Sitzungen widerrufen können** · offen
`web/auth.py` (`SessionCodec`) speichert die Sitzung zustandslos als signierten Cookie, 30 Tage gültig.
Ein Logout löscht den Cookie nur im Browser, ein kopierter Cookie bleibt gültig.
Gemeint ist: eine Sitzungsversion oder Epoche, die beim Logout oder per Befehl hochgezählt und im
Cookie geprüft wird (z. B. in einer kleinen Tabelle oder Datei im Datenordner). Alternativ ein Befehl
`wristpull serve --rotate-secret`, der alle Sitzungen beendet.

**4. Ende-zu-Ende-Test fürs Dashboard** · offen
Die Tests nutzen Starlette's `TestClient`. Er prüft Seiten und Routen, aber nicht das htmx-Verhalten im Browser
(Job-Polling, Partial Swaps, CSRF-Header aus JavaScript).
Gemeint ist: ein Playwright-Smoke-Test (opt-in wie der Live-Test, z. B. `WRISTPULL_E2E=1`) mit
synthetischen Daten und `FakeBackend`, der Login → Coach-Lauf → Bewertung → Workout speichern durchklickt.

### B. Release und Einstieg für neue Nutzer

**5. Echtes Release** · offen
Version `0.2.0` in `pyproject.toml`, aber keine Git-Tags, kein CHANGELOG, kein PyPI-Paket.
Gemeint ist: `CHANGELOG.md` (Keep-a-Changelog-Stil), ein Tag wie `v0.3.0` mit GitHub Release Notes und
Veröffentlichung auf PyPI, damit `uv tool install wristpull` bzw. `pipx install wristpull` funktioniert.
Vorher prüfen, ob der Name auf PyPI frei ist. Das `.mo`-File und die Web-Templates/Static-Files müssen im
Wheel landen.

**6. CI auf GitHub** · offen
Unter `.github/` liegen nur die Issue-Templates.
Gemeint ist: ein GitHub-Actions-Workflow, der bei Push und Pull Request `uv sync --all-extras`, `pyflakes` und
`pytest` ausführt, dazu ein Badge im README und auf der Website. Später erweitern um Release (Punkt 5) und
Image-Build (Punkt 7).

**7. Fertiges Container-Image** · offen · *wartet auf den Docker-Umbau*
Gemeint ist: Die CI baut das Image (amd64 und arm64) und veröffentlicht es auf GHCR
(`ghcr.io/one7two99/wristpull`). Dann reicht `docker compose pull` statt selbst zu bauen. Wichtig für den
geplanten Home-Server.

**8. `wristpull doctor`** · offen
Gemeint ist: ein Befehl, der typische Einrichtungsfehler erkennt und jeweils den konkreten Fix nennt:
- Garmin Connect App noch gekoppelt bzw. synchronisiert (Sync-Rennen, siehe README „Sources and how they relate“)
- ADB nicht autorisiert oder mehrere Geräte ohne `serial`
- Gadgetbridge-Intent-API oder Auto-Export nicht aktiv
- kiod6 hält das MTP-Gerät
- Workout-Limit der Uhr fast erreicht
- Ollama nicht erreichbar, API-Key fehlt
- Coach-Profil hat noch Platzhalter
- Rechte im Datenordner, Zeitzone gesetzt

Exit-Code ungleich 0 bei Fehlern. Nach dem Docker-Umbau zusätzlich prüfen: Container sieht USB, Gruppenrechte.

**9. Demo-Modus `wristpull demo`** · offen · *größter Hebel für die Verbreitung*
Gemeint ist: aus den vorhandenen synthetischen Generatoren (`tests/fitwriter.py`, `tests/gb_db.py`,
`build_*_fit(day)`) eine Demo-Datenbank über z. B. 8 Wochen erzeugen, mit plausiblen Verläufen,
Aktivitäten, Nächten und ein paar Coach-Läufen. Damit kann man TUI, Dashboard, Export und Coach ohne
Uhr ausprobieren. Separater Datenordner (z. B. `~/.local/share/wristpull-demo`), damit nie echte Daten
vermischt werden. Ist auch die Grundlage für Punkt 15 (Screenshots/Live-Demo).

**10. Linux-Hinweis und Gerätematrix im README** · offen
Das README sagt nirgends explizit, dass das Tool nur unter Linux läuft (gio, gvfs, MTP, kiod6). Der Install-Abschnitt beginnt mit
`cd ~/dev/wristpull`.
Gemeint ist: ganz oben „Linux only“ mit Begründung, dazu eine Gerätetabelle mit den Stufen *verifiziert / sollte
gehen / unbekannt*. Den Install-Abschnitt auf `uv tool install` (nach Punkt 5) und Docker (nach dem Umbau)
umstellen. Optional: `wristpull inspect --report`, das eine anonymisierte Ausgabe für das
Parser-Issue-Template erzeugt.

### C. Website (`docs/index.html`)

**11. Schriften selbst hosten** · offen · *vor dem Launch*
`docs/index.html` lädt Sora und IBM Plex von `fonts.googleapis.com` (Zeile 10–11).
`docs/logos/wristpull-g-logo.svg` lädt Sora per `@import` ebenfalls von Google. Das widerspricht der
Aussage „0 trackers“ und ist in Deutschland ein bekannter Abmahngrund (LG München, 2022).
Gemeint ist: WOFF2-Dateien unter `docs/fonts/` mit `@font-face`, den Text im Logo-SVG in Pfade
umwandeln. Danach prüfen, dass die Seite keine externen Requests mehr macht.

**12. Vorschau-Tags (Open Graph / Twitter Card)** · offen
Es gibt nur `<meta name="description">`.
Gemeint ist: `og:title`, `og:description`, `og:image` (1200×630, z. B. Logo plus Dashboard-Screenshot),
`og:url` und `twitter:card=summary_large_image`. Dann erscheint beim Teilen auf Mastodon, Reddit und HN
eine Vorschau.

**13. Impressum und Datenschutzerklärung** · offen
Gemeint ist: zwei kleine statische Seiten, verlinkt im Footer. Spätestens bei eigener Domain oder Spendenlink
sinnvoll. Die Datenschutzerklärung ist kurz, wenn die Seite wirklich keine externen Ressourcen lädt
(Punkt 11). Hinweis: keine Rechtsberatung, im Zweifel prüfen lassen.

**14. Quickstart ganz oben** · offen
Gemeint ist: drei bis vier Befehle bis zum ersten Ergebnis direkt im Hero-Bereich (installieren → Demo
→ TUI oder Dashboard). Die lange Befehlsreferenz kann aus dem README in eine eigene Doku-Seite wandern
(z. B. MkDocs unter `/docs`), damit die Landingpage schlank bleibt.

**15. Dashboard-Screenshots und Live-Demo** · offen
Die Website zeigt bisher nur TUI-Screenshots (`docs/screenshots/01–10*.svg`).
Gemeint ist: Dashboard-Screenshots (Handy und Desktop, hell und dunkel) automatisch per Playwright aus der
Demo-Datenbank (Punkt 9) erzeugen, wie bei der TUI pseudonymisiert. Später optional eine statische
Live-Demo mit Fake-Daten auf GitHub Pages.

### D. Dashboard

**16. Warnung bei veralteten Daten** · offen
Gemeint ist: ein deutlicher Banner auf der Today-Seite (und ein Hinweis im Coach-Datenpaket), wenn
der letzte Import von Uhr oder Handy älter als ein konfigurierbarer Schwellwert ist (z. B. 24 bzw. 36 h). Sonst sehen
Philipp und der Coach unbemerkt alte Werte. Datenbasis: `status.collect_status` bzw. die letzten
`raw_files`-Zeitstempel pro Quelle.

**17. Kalender: Plan gegen tatsächliches Training** · teilweise
Vorhanden: Plan-Adherence im wöchentlichen Coach-Review, ICS-Export des Plans.
Gemeint ist: eine Wochen- oder Monatsansicht im Dashboard, die geplante Einheiten (Wochenplan bzw.
Workout-Dateien) neben die echten Aktivitäten stellt, farbig nach erledigt / verpasst / abweichend.

**18. Tagebuch und Coach-Tipps in den Kurven** · offen
Gemeint ist: Journal-Einträge und Coach-Läufe als Markierungen (ECharts `markPoint`/`markLine`) in den
Trend-Diagrammen, mit Tooltip. Dann sieht man direkt, dass der Ruhepuls nach „erkältet“ oder „spät gegessen“
hochging.

**19. Übersicht der Datenlücken** · teilweise
Vorhanden: Lücken werden als `null` gezeichnet („Missing is not zero“).
Gemeint ist: eine Heatmap Tag × Quelle (`garmin_usb`, `gadgetbridge_garmin`, `gadgetbridge_zepp`) und
gegebenenfalls Metrik, damit Lücken durch das Sync-Rennen sofort auffallen.

**20. Als App aufs Handy installierbar (PWA)** · offen
Vorhanden: `web/static/icon.svg`, HTTPS über Caddy (Voraussetzung für PWA).
Gemeint ist: `manifest.webmanifest` mit Icons in den nötigen Größen, `theme-color` und optional ein
Service Worker, der die letzte Today-Ansicht offline anzeigt. Die CSP muss den Service Worker zulassen
(`worker-src 'self'`).

### E. Analyse

**21. Abweichungsreport Uhr gegen Strap** · teilweise
Vorhanden: Ruhepuls von Uhr und Strap in den Trends, Uhr gegen Strap in der Nacht-Detailansicht.
Gemeint ist: ein Report pro Kennzahl (Ruhepuls, HRV, Schlafdauer, Schlafphasen) mit mittlerer
Abweichung und Streuung, z. B. als Bland-Altman-Diagramm, als `report devices` und als Dashboard-Seite.
Philipp trägt beide Geräte gleichzeitig, das hat kaum jemand. Guter Stoff für einen Launch-Blogpost.

**22. Robusteres Belastungsmodell** · offen
Vorhanden: `coach/metrics.acute_chronic` (rollende 7/28-Tage-Mittel), im Dashboard mit Band 0,8–1,3.
Die ACWR mit rollenden Mitteln ist fachlich umstritten (u. a. weil die akute Last Teil der chronischen ist).
Gemeint ist: zusätzlich eine EWMA-Variante und/oder das Banister-Modell CTL/ATL/TSB (Fitness, Ermüdung,
Form), das Ausdauersportler aus anderen Tools kennen. Deterministisch in Python berechnen und an den
Coach weitergeben.

**23. HRV-Normalbereich** · offen
Gemeint ist: eigene Baseline ± Standardabweichung über etwa 60 Tage als Band (ähnlich wie bei HRV4Training), im
Dashboard und im Coach-Paket. Erst ein Wert außerhalb des Bands wird als Signal gewertet. Gleiches Prinzip
optional für den Ruhepuls.

**24. Waage als Datenquelle** · offen
Gemeint ist: Gewicht und Körperfett aus Gadgetbridge-unterstützten Bluetooth-Waagen (z. B. Mi Scale) als
neue Quelle bzw. Tabelle, angezeigt im Dashboard und im Coach-Kontext. Passt zu Philipps Gewichtsziel.
Alternativ eine manuelle Eingabe im Dashboard.

### F. Quellen und Anbindungen

**25. Import des Garmin-Connect-Datenexports** · offen
Gemeint ist: Import des DSGVO-Exports von Garmin Connect (Zip mit FIT-Dateien und JSON-Zusammenfassungen,
oft mehrere Jahre). Rohdateien laufen durch denselben Parser, JSON-Tageswerte werden als eigene Quelle
geführt. Starker Umstiegsgrund für Connect-Nutzer, und die Coach-Baselines stimmen dann vom ersten Tag an.

**26. MCP-Server** · offen
Gemeint ist: `wristpull mcp`, ein schreibgeschützter MCP-Server (stdio) über die SQLite mit festen Tools
(`get_days`, `get_activities`, `get_sleep`, `get_trends`, `get_coach_runs`), aufgebaut auf den vorhandenen
Aggregatfunktionen. Kein freies SQL. Dann kann Claude Desktop oder Claude Code die Daten direkt abfragen.
Steht in der README-Roadmap unter „Later“.

**27. Weitere Gadgetbridge-Geräte** · offen
Gemeint ist: weitere Zepp-, Xiaomi- oder Bangle.js-Geräte, deren Daten in ähnlichen Tabellen der
Gadgetbridge-Datenbank liegen. Jedes Gerät erweitert die Zielgruppe relativ günstig. Ohne eigenes Gerät
nur mit Test-Exporten von Nutzern möglich, daher Hand in Hand mit Punkt 10 (Gerätematrix,
`inspect --report`).

**28. `wristpull ship` für den Server-Umzug** · offen · *mit dem Docker-Umbau abstimmen*
Basis ist der Abschnitt „Moving to a server“ in `docs/dashboard-concept.md`: Der Server wird die einzige
Datenbank, der Laptop verschickt nur Rohdateien.
Gemeint ist: `ship` überträgt die neuen Rohdateien per rsync über SSH auf den Server. Dort startet eine
systemd-Path-Unit (oder ein Watch-Ordner) den Import aus Dateien. Der Workout-Push holt die
Markdown-Dateien vorher vom Server. Ablauf: Uhr ans Kabel, danach ist alles auf dem Server.

**29. Coach-Check als Nachricht aufs Handy** · offen
Gemeint ist: Der tägliche Coach-Check (Timer 07:00) geht zusätzlich per ntfy (selbst gehostet möglich) oder
Telegram raus, als kurze Zusammenfassung mit Link ins Dashboard. Konfigurierbar unter `[notify]`.

**30. Anbindung an Home Assistant** · offen
Gemeint ist: Ruhepuls, HRV, Body Battery, Schlafscore und Schritte als Sensoren per MQTT (mit
Home-Assistant-Discovery) veröffentlichen, z. B. nach jedem Import. Damit sind Automationen oder eine
Anzeige im Flur möglich.

### Erledigt

- Dashboard nur mit Login und HTTPS erreichbar: umgesetzt mit Caddy `tls internal`, scrypt, CSRF,
  Rate-Limit, CSP. Gründlicher als ursprünglich vorgeschlagen.

---

## 4. Empfohlene Reihenfolge

1. **Sofort:** Punkt 2 (Zertifikat) und Punkt 1 (Backup).
2. **Release-Paket:** 6 (CI) → 5 (Release/PyPI) → 9 (Demo) → 8 (doctor) → 10 (README).
   Punkt 7 erst nach dem Docker-Umbau.
3. **Vor dem Launch, Website:** 11 → 12 → 13 → 14 → 15.
4. **Danach nach Nutzen:** 16 (veraltete Daten), 3 (Sitzungen), 4 (E2E-Test), dann Analyse (22, 23, 21)
   und Anbindungen (26 MCP, 25 Connect-Import, 29, 30), 28 zusammen mit dem Server-Umzug.

## 5. Vorschlag für den Start im neuen Chat

- Repo neu einlesen (der Stand kann sich seit `34e26a7` geändert haben) und den Status der Punkte prüfen.
- Dann pro Paket eine **Work-Order für Claude Code** schreiben: Ziel, betroffene Dateien, Leitplanken
  aus Abschnitt 2, Tests, Akzeptanzkriterien, Doku-Updates.
- Erstes Paket: Punkte 2 + 1.
