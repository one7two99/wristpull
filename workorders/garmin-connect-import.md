# Work-Order: Import des Garmin-Connect-Datenexports (Punkt 25)

Stand: 2026-11-07, Repository-Stand Commit `7a1fbc7` (contrib/bin/wristpull-morning).
Repository: <https://github.com/one7two99/wristpull> (Branch `master`).
Zweck: Umsetzung in einer eigenen Claude-Code-Session. Vor dem Start das Repo neu einlesen und prüfen,
ob sich die genannten Stellen geändert haben. Der Docker-Umbau (MTP-Schicht, `cli`-Service) ist
abgeschlossen und wird durch diesen Auftrag nicht berührt.

Gespräch mit Philipp auf **Deutsch**, Code/Kommentare/Commits/README auf **Englisch**.

---

## 1. Ziel

`wristpull import garmin-export PATH` importiert den DSGVO-Datenexport von Garmin Connect
(das Portal-ZIP, unverändert wie heruntergeladen, alternativ ein bereits entpacktes Verzeichnis).
Die FIT-Dateien aus dem Export laufen durch den vorhandenen Parser, die JSON-Tageswerte und
Schlafdaten werden als eigene Quelle `garmin_connect` geführt.

Nutzen:
- **Backfill der kompletten Historie** von vor wristpull (bei Philipp: zurück bis 2015/2016).
- **Cloud-only-Werte**, die nie per FIT auf der Uhr landen: Sleep Score, Aktivitätsnamen.
- Starker Umstiegsgrund für Connect-Nutzer; die Coach-Baselines stimmen vom ersten Tag an.

## 2. Ausgangslage

### Struktur des Exports (am echten Export von Philipp verifiziert, 2026-11-07)

Vom Portal-ZIP ist nur `DI_CONNECT/` relevant (der Rest sind leere Garmin-Sparten). Darin:

| Ordner | Inhalt | Relevanz |
|---|---|---|
| `DI-Connect-Uploaded-Files/` | `UploadedFiles_*.zip` (ggf. mehrere Parts), darin **15.470 FIT-Dateien** (77 MB entpackt). Stichprobe über 250 Dateien: ~63 % `monitoring_b`, Rest FIT-Typen 49, 44, 41, 58, 68 und `activity`. Es können weitere ZIPs **verschachtelt** darin liegen (eine Ebene). | Phase 1, Kernstück |
| `DI-Connect-Aggregator/` | `UDSFile_<von>_<bis>.json` — Daily Summaries in ~100-Tage-Blöcken (bei Philipp 24 Dateien, 1.472 Tage, 2015–2026). Dazu `HydrationLogFile_*.json`. | Phase 2 |
| `DI-Connect-Wellness/` | `<von>_<bis>_<userPK>_sleepData.json` (2015–heute, inkl. Schlafphasen-Summen und `sleepScores`), `*_AbnormalHrEvents.json`, `*_nutritionLogs.json`, Biometrie/HR-Zonen-JSONs | Phase 2 (nur sleepData) |
| `DI-Connect-Fitness/` | `*_summarizedActivities.json` (262 Aktivitäts-Summaries 2016–2026 mit Cloud-Feldern: Name, Training Load, TE), Gear, Personal Records, Trainingspläne | Phase 3 |
| `DI-Connect-Metrics/` | `MetricsMaxMetData_*` (VO2max-Historie), `TrainingHistory_*`, `RunRacePredictions_*`, Fitness Age | Phase 3 |

Beobachtete UDS-Felder (Auszug): `calendarDate`, `totalSteps`, `totalDistanceMeters`,
`activeKilocalories`, `restingHeartRate`, `minHeartRate`/`maxHeartRate`,
`moderateIntensityMinutes`/`vigorousIntensityMinutes`, `allDayStress` (Objekt),
`bodyBattery` (Objekt), `averageSpo2Value`/`lowestSpo2Value`, `respiration` (Objekt),
`wellnessStartTimeGmt`/`...Local`. Sleep-Felder: `sleepStartTimestampGMT`/`sleepEndTimestampGMT`,
`deepSleepSeconds`/`lightSleepSeconds`/`remSleepSeconds`/`awakeSleepSeconds`/`unmeasurableSeconds`,
`sleepScores`, `averageRespiration`, `avgSleepStress`, `retro`, `calendarDate`.

**Achtung, Datenmüll:** Es gibt UDS-Blöcke mit Datumsangaben 1989/1990, 2005 und 2012 — das ist
die FIT-Epoche (1989-12-31) als Platzhalter für kaputte Zeitstempel. Diese Zeilen verwerfen.

**Die inneren Objektstrukturen** (`allDayStress`, `bodyBattery`, `respiration`, `sleepScores`) **am
echten Export verifizieren, bevor der Parser geschrieben wird.** Philipp stellt das ZIP in der
Session lokal bereit; es kommt selbstverständlich nie ins Repo.

### Anknüpfungspunkte im Code

- `importers/garmin.py`: `import_file(conn, archive, data, *, kind, original_name, …, source=…)`
  archiviert + parst **eine FIT-Datei aus Bytes** — exakt die Schnittstelle, die der ZIP-Walker braucht.
  `store_parsed` schreibt `ParsedRows` mit Dedup je Tabelle. `ImportStats` wiederverwenden.
- `archive.py`: globale SHA-256-Dedup in `raw_files` über `UNIQUE(sha256)` — Dateien, die bereits
  per USB/MTP von der fenix geholt wurden, sind byte-identisch und werden automatisch übersprungen.
- `rows.py`: `ParsedRows`/`ParsedSleep` für den Sleep-JSON-Parser wiederverwendbar.
- `daily.py`: `rebuild_daily_summary` arbeitet je Quelle über die Sample-Tabellen; neue Quellen
  fließen automatisch ein. Hier kommt das Overlay für die UDS-Tageswerte dazu (siehe Entscheidung 3).
- `merge.py`: `GARMIN_PRIORITY = ("garmin_usb", "gadgetbridge_garmin")` erweitern.
- `db.py`: `MIGRATIONS`-Mechanismus (`ensure_columns`) für neue Spalten, `SCHEMA_VERSION` hochzählen.
- `importers/reparse.py`: `delete_rows_of` + Re-Parse je `raw_file_id` — die JSON-Parser müssen
  hier mitspielen (Dispatch bisher nur FIT und Gadgetbridge-DB).
- `garmin_usb.py`: `FOLDER_KINDS` als Vorbild für die Typ-Zuordnung ohne Ordnerkontext.

## 3. Entscheidungen

1. **Eine Quelle `garmin_connect`** für alles aus dem Export (FIT wie JSON). Merge-Priorität:
   `("garmin_usb", "gadgetbridge_garmin", "garmin_connect")` — der Export füllt nur Lücken,
   live gezogene Daten gewinnen immer.
2. **Kind-Ableitung ohne Ordner:** Im Export gibt es keine `GARMIN/`-Ordnerstruktur. Der Kind
   (`activity | monitor | sleep | metrics | hrv | other`) wird aus dem dekodierten FIT-Dateityp
   abgeleitet (Zuordnungstabelle analog `FOLDER_KINDS`, z. B. `monitoring_b → monitor`).
   Die numerischen Typen (44, 49, 41, 58, 68) vorab mit `wristpull inspect` an Dateien aus dem
   echten Export verifizieren und nur die zuordnen, deren Bedeutung belegt ist; unbekannte → `other`.
   `import_file` dekodiert bereits — doppeltes Dekodieren vermeiden (kleine Refaktorierung ist in
   Ordnung, Signatur-Kompatibilität für die bestehenden Aufrufer wahren).
3. **UDS-Tageswerte gehen in eine eigene Tabelle `connect_daily`**, nicht in die Sample-Tabellen.
   Begründung: UDS liefert Tagesaggregate (z. B. Stress-Durchschnitt), keine Zeitreihen; ein
   Aggregat als Pseudo-Sample würde `summarize_day` verfälschen. Spalten spiegeln die
   Metrik-Spalten von `daily_summary` (steps, distance_m, active_calories, moderate_min,
   vigorous_min, hr_min/max, resting_hr, stress_avg/max, body_battery_min/max, spo2_avg/min,
   respiration_avg, …) plus `source`, `local_date`, `tz`, `raw_file_id`,
   `UNIQUE(source, local_date)`. Bei Blocküberlappungen gewinnt deterministisch die später
   verarbeitete Datei (Dateien sortiert verarbeiten, `INSERT OR REPLACE`).
   `rebuild_daily_summary` legt danach ein **Overlay**: aus Samples berechnete Werte gewinnen,
   `connect_daily` füllt nur NULL-Spalten des jeweiligen Tages (betrifft praktisch nur die Quelle
   `garmin_connect` an Tagen ohne Monitoring-FITs).
4. **Sleep-JSONs werden echte `sleep`-Zeilen** (sie sind Sessions mit Start/Ende): GMT-Timestamps →
   `start_utc`/`end_utc`, Phasen-Summen → `deep_s`/`light_s`/`rem_s`/`awake_s`/`unmeasurable_s`,
   `sleepScores` → `score`, `avgSleepStress` → `avg_stress`. Keine `sleep_stages`-Zeilen (der Export
   enthält keine Phasen-Timeline). Die Dedup über `UNIQUE(source, start_utc)` greift wie gehabt.
5. **Plausibilitätsfilter:** Tage vor 2013 verwerfen (FIT-Epoche-Platzhalter), mit Zähler in der
   Statistik ausweisen, nicht stillschweigend.
6. **Zeiten:** GMT-Felder des Exports → ISO-UTC mit `Z`; `local_date` aus `calendarDate`, wo der
   Export es liefert, sonst aus GMT + konfigurierter Zeitzone. Historisch abweichende Zeitzonen
   werden bewusst nicht rekonstruiert (im README-Abschnitt erwähnen).
7. **ZIP-Verarbeitung streamend:** `zipfile` direkt auf dem Portal-ZIP und den inneren
   `UploadedFiles_*.zip`, Datei für Datei als Bytes an `import_file` — kein vollständiges Entpacken
   auf Platte, kein Staging nötig. Verschachtelte ZIPs eine Ebene tief verfolgen.
8. **Out of scope:** Hydration, Nutrition (gehört thematisch zu Punkt 24/Waage bzw. eigener Punkt),
   Biometrie/Gewicht, Gear, Trainingspläne, AbnormalHrEvents. Nicht importieren, nicht archivieren.

## 4. Leitplanken

Alle Regeln aus `CLAUDE.md` gelten, insbesondere:
- Gesundheitsdaten nie ins Repo. Tests nutzen ausschließlich einen **synthetischen Export-Builder**
  (neu, z. B. `tests/connect_export.py`): baut ein Mini-Portal-ZIP mit UDS-JSON, Sleep-JSON und
  einem inneren `UploadedFiles_*.zip` aus synthetischen FITs (`wristpull.fit.writer`). Nie Hardware,
  nie Netzwerk.
- Jede neue Funktion bekommt einen Test. Vor jedem Commit `uv run pyflakes src tests && uv run pytest -q`.
- Rohdateien unveränderlich: auch die JSON-Dateien des Exports landen im Archiv (`raw_files`),
  damit `reparse` nach Parser-Änderungen funktioniert.
- Zeitstempel UTC ISO-8601 mit `Z`, dazu `tz` und `local_date` auf jeder Zeile.
- TUI und Web rufen nur Bibliotheksfunktionen auf (Boundary-Tests bleiben grün).
- Neue Strings in TUI/Web über `_()`, deutschen Katalog aktualisieren, `.mo` neu kompilieren.
- Nach jeder Phase: README aktualisieren, Tests grün, Commit.

## 5. Umsetzung in Phasen

### Phase 1 — ZIP-Walker + FIT-Pfad

- `wristpull import garmin-export PATH [--dry-run]`: nimmt das Portal-ZIP oder ein entpacktes
  Verzeichnis, findet `DI_CONNECT/` selbst, iteriert die `UploadedFiles_*.zip` streamend
  (Entscheidung 7), klassifiziert per FIT-Typ (Entscheidung 2) und importiert über
  `import_file(…, source="garmin_connect")`. Danach `rebuild_daily_summary`.
- Fortschrittsausgabe: 15.470 Dateien dauern; mindestens ein Zähler alle n Dateien, `-q` respektieren.
  Commit-Verhalten prüfen: ein Commit pro Datei ist bei 15k Dateien spürbar — batchen ist erlaubt,
  solange ein Abbruch keinen inkonsistenten Zustand hinterlässt (Archiv-Datei ohne `raw_files`-Zeile
  darf nicht vorkommen).
- `GARMIN_PRIORITY` erweitern (Entscheidung 1). Danach **alle Stellen mit Quellen-Aufzählungen
  prüfen**: `merge.py`, `status.py`, `aggregate.py`, Export/Report, TUI-Screens, Web-Seiten,
  Schema-Kommentar in `schema.sql` (Zeile 3). Die Quelle muss in `status` und im Dashboard sichtbar
  sein, ohne Layouts zu sprengen.
- Tests: Walker gegen den synthetischen Export-Builder; Kind-Zuordnung; Dedup (zweiter Lauf
  importiert nichts; eine Datei, die schon als `garmin_usb` archiviert ist, wird übersprungen).
- Akzeptanz am echten Export (lokal mit Philipp): Lauf 1 importiert, Lauf 2 meldet 0 neue Dateien;
  Stichprobe: eine Aktivität aus 2017 erscheint in TUI/Dashboard.

### Phase 2 — JSON-Parser (UDS + Sleep)

- Neues Modul, z. B. `src/wristpull/garmin_connect.py`: `parse_uds(bytes)` → `connect_daily`-Zeilen,
  `parse_sleep_json(bytes)` → `ParsedRows` mit `ParsedSleep` (Entscheidungen 3–5). Die inneren
  Objektstrukturen vorher am echten Export verifizieren.
- JSON-Dateien archivieren (`raw_files`): eigene Kind-Werte festlegen (z. B. `uds` und `sleep`;
  den Kommentar in `schema.sql` nachziehen) — so kann `reparse --kind` sie gezielt treffen.
- Schema: Tabelle `connect_daily` in `schema.sql`, `SCHEMA_VERSION` hochzählen.
- `rebuild_daily_summary`: Overlay aus `connect_daily` (Entscheidung 3).
- `reparse` erweitern: Dispatch für die beiden JSON-Formate; `delete_rows_of` muss auch
  `connect_daily`-Zeilen über `raw_file_id` löschen.
- Tests: Parser gegen synthetische JSONs (inkl. 1989er-Müllzeilen → verworfen und gezählt;
  Blocküberlappung → letzte Datei gewinnt; Overlay: Sample-Wert gewinnt gegen UDS-Wert).
- Akzeptanz am echten Export: Tage aus 2016 zeigen Schritte/Ruhepuls im Dashboard; eine Nacht aus
  2015 hat Schlafphasen-Summen und Score; `daily_summary` für einen aktuellen Tag ändert sich
  **nicht** (live-Quellen gewinnen).

### Phase 3 — Enrichment (optional, eigener Commit-Block)

- `summarizedActivities.json`: bestehende Aktivitäten (aller Quellen!) per Startzeit-Toleranz
  (± 2 min) und Sportart matchen und den **Cloud-Namen** übernehmen — neue nullable Spalte
  `activities.name` über `MIGRATIONS`. Nur füllen, wenn NULL; nie bestehende Werte überschreiben.
  Anzeige in TUI-Aktivitätenliste, Dashboard und Export prüfen.
- `MetricsMaxMetData_*` → `metrics`-Tabelle (`vo2_max`, Quelle `garmin_connect`), damit die
  VO2max-Historie vor wristpull sichtbar wird.
- Wenn der Aufwand aus dem Ruder läuft: Phase 3 abbrechen und als Folgepunkt notieren — Phasen 1+2
  sind der Wert dieses Auftrags.

## 6. Risiken

- **Undokumentiertes JSON-Schema:** Garmin ändert das Exportformat gelegentlich; Feldnamen
  variieren zwischen Export-Generationen. Parser defensiv schreiben (fehlende Felder → NULL,
  nie Absturz), unbekannte Strukturen zählen und am Ende zusammenfassen.
- **FIT-Dateien mit Quirks:** 15k Dateien aus zehn Jahren und mehreren Firmware-Generationen —
  einzelne Parse-Fehler sind zu erwarten. Der bestehende Fehlerpfad (`parse_status='error'`,
  Import läuft weiter) deckt das ab; Fehlerquote in der Abschlussmeldung ausweisen.
- **Laufzeit/Speicher:** Streamen statt entpacken, Batching beim Commit, keine ganzen
  ZIP-Inhalte in den Speicher laden.
- **Doppelte Aktivitäten über Quellen hinweg:** Die Activity-Dedup je Quelle greift; über Quellen
  hinweg dedupliziert `merge.py` nach Startzeit wie bisher (Garmin-Gerätesicht). Mit echten Daten
  stichprobenartig prüfen, dass eine Aktivität nicht doppelt in der Gerätesicht erscheint.

## 7. Doku-Updates

- `README.md`: `import garmin-export` in der Befehlstabelle; neuer Abschnitt „Importing a Garmin
  Connect export" (wo man den Export bei Garmin anfordert, was importiert wird, was nicht,
  Zeitzonen-Hinweis, Hinweis auf Quelle `garmin_connect` und Merge-Priorität).
- `CLAUDE.md`: Quelle `garmin_connect`, `connect_daily` + Overlay, Kinds `uds`/`sleep`,
  synthetischer Export-Builder für Tests.
- `schema.sql`: Quellen- und Kind-Kommentare aktualisieren.
- Projektseite (`docs/index.html`): Gerätetabelle um „Garmin Connect account export" ergänzen
  (Status: done), ein Satz im Feature-Teil.

## 8. Definition of Done

- `wristpull import garmin-export <portal.zip>` läuft nativ **und** im `cli`-Container
  (`docker compose run --rm cli …`) durch; zweiter Lauf importiert nichts.
- Am echten Export bestätigt: Historie ab 2015/2016 in TUI und Dashboard sichtbar, Sleep Scores
  vorhanden, aktuelle Tage unverändert (Merge-Priorität), USB-Duplikate übersprungen.
- `reparse` kann die JSON-Dateien erneut parsen.
- Alle Tests grün (alte und neue, ohne echte Daten), pyflakes sauber.
- Doku aktualisiert, pro Phase ein Commit.
