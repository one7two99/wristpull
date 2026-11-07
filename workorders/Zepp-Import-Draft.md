# Draft: Import des Zepp-/Amazfit-Cloud-Exports (liegt auf Eis)

Stand: 2026-11-07. Kein Auftrag, sondern ein Gedächtnisstück für den Fall, dass das Thema wieder
aufkommt. Repository-Stand Commit `193383b` (Garmin-Connect-Import und Docker-`cli` sind fertig).

## 1. Warum es gerade nicht lohnt

Philipp hat am 2026-11-07 den DSGVO-Export seines Zepp-Kontos angefordert und geprüft (ZIP, AES-
verschlüsselt, Passwort kommt separat per Mail; 22 Dateien, 675 Byte). Ergebnis: **alle Datendateien
enthalten nur die Kopfzeile.** Befüllt sind nur `USER` (Profil: Größe, Gewicht, Geburtsmonat,
Nickname) und `BODY` mit genau einem Eintrag (Profilgewicht 70,0 kg vom 2026-10-31, keine Messung).

Grund: Der Amazfit Helio Strap wurde nie mit der Zepp-App synchronisiert, sondern von Anfang an mit
Gadgetbridge. Alles, was der Strap je geliefert hat, steckt bereits in wristpull unter der Quelle
`gadgetbridge_zepp` (`wristpull pull phone`). Ein Importer hätte nichts zu holen.

Wieder aufgreifen, wenn
- ein Amazfit-/Zepp-Gerät zeitweise mit der Zepp-App statt mit Gadgetbridge läuft, oder
- Daten eines früheren Zepp-/Mi-Fit-Kontos (anderes Gerät, andere Jahre) auftauchen.

## 2. Format des Exports (am leeren Export verifiziert, Spalten also belegt, Werteformate nicht)

ZIP mit einem Ordner je Datentyp, darin eine CSV `<TYP>_<ms-timestamp>.csv`, UTF-8 mit BOM,
Komma-getrennt, `null` für fehlende Werte. Zeiten in `BODY` als `YYYY-MM-DD HH:MM:SS+0000` (UTC);
in den anderen Dateien `date` (lokaler Tag) plus `time` – ob `time` lokal oder UTC ist, am
befüllten Export prüfen.

| Ordner | Spalten | Ziel in wristpull |
|---|---|---|
| `ACTIVITY` | `date,steps,distance,runDistance,calories` | Tagesaggregat → eigene Tabelle (analog `connect_daily`) oder nur `daily_summary`-Overlay |
| `ACTIVITY_MINUTE` | `date,time,steps` | `steps` (Minutenwerte; Zeitzone klären) |
| `ACTIVITY_STAGE` | `date,start,stop,distance,calories,steps` | Phasen; vermutlich verzichtbar |
| `HEARTRATE` | `time,heartRate` | `heart_rate` (manuelle/Einzelmessungen) |
| `HEARTRATE_AUTO` | `date,time,heartRate` | `heart_rate` (automatische Messungen, der Hauptstrom) |
| `SLEEP` | `date,deepSleepTime,shallowSleepTime,wakeTime,start,stop,REMTime,naps` | `sleep` (Zeiten vermutlich in Minuten, `start`/`stop` als Epoch-Sekunden – prüfen) |
| `SLEEP_MINUTE` | `date,time,stage,hr,respiratory_rate` | `sleep_stages` (Stage-Codes mit Gadgetbridges Huami-Codes vergleichen: 4 light, 5 deep, 7 awake, 8 rem) und `respiration` |
| `SPORT` | `type,startTime,sportTime(s),maxPace(/meter),minPace(/meter),distance(m),avgPace(/meter),calories(kcal)` | `activities` (ohne Records; `type` ist eine Zepp-Sportart-Nummer, Tabelle aus Gadgetbridge `HuamiSportDataType`) |
| `BODY` | `time,weight,height,bmi,fatRate,bodyWaterRate,boneMass,metabolism,muscleRate,visceralFat` | `metrics` `weight_kg` (nur echte Messungen, Profilgewicht erkennen) |
| `HEALTH_DATA` | `date,time,arm,calf,chest,hip,thigh,waist` | Körpermaße; out of scope |
| `USER` | `userId,gender,height,weight,nickName,avatar,birthday` | nicht importieren (Profil, personenbezogen) |

Nicht im Export (soweit am leeren Export erkennbar): SpO2, Stress, HRV, PAI. Ob der Strap diese
Werte in die Cloud schreibt, zeigt erst ein befüllter Export.

## 3. Entwurf

- **Befehl:** `wristpull import zepp-export PATH` (ZIP oder entpacktes Verzeichnis). Die
  Entschlüsselung bleibt beim Nutzer (`7z x -p…`), oder der Befehl nimmt `--password`; die
  Python-`zipfile` kann kein AES, also entweder `7z` als Systemabhängigkeit (auch ins Docker-Image)
  oder das entpackte Verzeichnis als einziger Weg.
- **Quelle:** `zepp_cloud`, Merge-Priorität für die Strap-Sicht `("gadgetbridge_zepp", "zepp_cloud")`
  – live gezogene Strap-Daten gewinnen, der Export füllt nur Lücken (wie `garmin_connect` beim
  Garmin-Pfad). `merge.py` kennt heute nur eine Strap-Quelle (`STRAP_SOURCE`); daraus wird eine
  Prioritätenliste wie `GARMIN_PRIORITY`.
- **Archiv:** jede CSV als Rohdatei in `raw_files` (Kinds z. B. `zepp_activity`, `zepp_heartrate`,
  `zepp_sleep`, `zepp_sport`, `zepp_body`), damit `reparse` nach Parser-Änderungen funktioniert;
  `reparse.JSON_KINDS` entsprechend um CSV-Kinds erweitern (Dispatch in `reparse_raw_file`).
- **Parser:** `src/wristpull/zepp_cloud.py` nach dem Muster von `garmin_connect.py`: jede Spalte
  optional, unbekannte Werte → NULL, Platzhalter verwerfen und zählen. Schlafphasen über
  `gadgetbridge.py` wiederverwenden (dort existiert die Huami-Stage-Zuordnung bereits).
- **Importer:** `src/wristpull/importers/zepp.py` nach `importers/connect.py`: ZIP in place lesen,
  Datei für Datei, SHA-256-Dedup, Commit pro Datei, `rebuild_daily_summary(source="zepp_cloud")`.
- **Tests:** synthetischer Export-Builder `tests/zepp_export.py` (CSV-Zeilen aus Python-Dicts, ZIP
  unverschlüsselt), keine echten Daten. Vor dem Parser die Werteformate am befüllten Export
  verifizieren (Zeitzonen, Minuten vs. Sekunden, Stage-Codes, Sportart-Nummern).
- **Sichtbarkeit:** Quelle in `status`, TUI-Schlaf-Screen (`SOURCE_GROUPS` → strap), Web
  (`SOURCE_LABELS`), README-Quellentabelle, `schema.sql`-Kommentar, `CLAUDE.md`.

## 4. Offene Fragen (nur am befüllten Export zu klären)

1. Zeitzone von `date`/`time` in `ACTIVITY_MINUTE`, `HEARTRATE_AUTO`, `SLEEP_MINUTE`.
2. Einheiten in `SLEEP` (Minuten?) und Format von `start`/`stop`.
3. Stage-Codes in `SLEEP_MINUTE` gegen Gadgetbridge (`HUAMI_SLEEP_SESSION_SAMPLE`).
4. Enthält der Export SpO2/Stress/HRV/PAI des Helio Strap irgendwo, oder bleibt das Gadgetbridge-
   exklusiv? Wenn letzteres, ist der Nutzen des Imports auf Schritte, Puls, Schlaf, Sport, Gewicht
   begrenzt.
5. Dubletten gegen `gadgetbridge_zepp`: dieselbe Nacht/dieselbe Minute aus beiden Quellen – reicht
   der Merge nach Startminute wie bei Garmin?

## 5. Aufwand (Schätzung)

Eine Session für Tageswerte, Puls, Schlaf und Gewicht; Sport und Schlafphasen je nach Format eine
zweite. Voraussetzung ist ein Export mit Inhalt – ohne den lohnt es nicht, auch nur den Parser zu
beginnen.
