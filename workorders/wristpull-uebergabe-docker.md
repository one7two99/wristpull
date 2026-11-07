# Übergabe / Work-Order: wristpull komplett im Docker-Container

Stand: 2026-11-07, Repository-Stand Commit `34e26a7` (2026-11-03 22:49).
Repository: <https://github.com/one7two99/wristpull> (Branch `master`).
Zweck: Umsetzung in einer eigenen Claude-Code-Session. Vor dem Start das Repo neu einlesen und prüfen,
ob sich die genannten Stellen geändert haben.

Gespräch mit Philipp auf **Deutsch**, Code/Kommentare/Commits/README auf **Englisch**. Philipp setzt
nichts manuell um: alles, was am Host zu tun ist (udev-Regeln, Units), wird als Datei unter `contrib/`
geliefert, mit kurzer Installationsanleitung in der jeweiligen README.

---

## 1. Ziel

**Alle** Funktionen von wristpull laufen aus einem Docker-Image, nicht nur das Web-Dashboard: Import von
der Uhr (USB/MTP), Pull vom Handy (ADB), TUI, Export, Report, Coach, Workouts (inklusive Push und Remove auf
der Uhr), `reparse`, `status`.

Nutzen:
- Auf dem Host sind kein Python 3.14 und kein uv mehr nötig.
- Laptop und späterer Home-Server nutzen dasselbe Image.
- Voraussetzung für das Veröffentlichen eines fertigen Images auf GHCR. Das ist ein separater Folgeauftrag,
  **nicht** Teil dieser Work-Order.

Die **native Installation per uv bleibt voll unterstützt** und ist der zweite gleichwertige Weg.

## 2. Ausgangslage

### Bereits vorhanden (Web-Dashboard D1–D3)

- `Dockerfile`: `python:3.14-slim`, uv `0.11.24`, `uv sync --frozen --no-dev --extra web --extra coach`,
  Non-Root-User. `CMD` startet `wristpull serve` mit **genau einem** uvicorn-Worker hinter `--behind-proxy`.
- `compose.yaml`: Service `web` (kein veröffentlichter Port, Daten-Mount rw, Config-Mount **ro**,
  `extra_hosts: host-gateway` für Ollama) und Service `caddy` (`tls internal`, Port 8443).
- `deploy/Caddyfile` und `.env.example` (UID/GID, Pfade, Secret, Passwort-Hash, API-Keys).
- `db.py` setzt `PRAGMA journal_mode = WAL`. Host-CLI und Container schreiben parallel auf denselben
  Bind-Mount, das funktioniert, weil beide auf demselben Kernel laufen.
- Konzept: `docs/dashboard-concept.md`. Dort stehen auch die Ollama-Regel (nie `OLLAMA_HOST=0.0.0.0`) und der
  spätere Server-Umzug.

### Host-Abhängigkeiten, die im Container nicht funktionieren

| Stelle | Abhängigkeit | Problem im Container |
|---|---|---|
| `src/wristpull/garmin_usb.py` (`gio_garmin_mtp_urls`, `try_mount_garmin`, `remount_garmin`, `eject_garmin`) | `gio mount`, gvfs, D-Bus der Desktop-Sitzung | gvfsd-mtp und der Session-Bus fehlen im Container |
| `garmin_usb.py` (`kde_holds_mtp`, `release_kde_mtp`) | `kquitapp6 kiod6` | greift in die KDE-Sitzung des **Hosts** ein, aus dem Container unmöglich |
| `src/wristpull/importers/garmin.py` (`locate_garmin`, `import_garmin`) | sucht einen gemounteten `GARMIN/`-Ordner (`candidate_roots`, `mounter`) | ohne gvfs-Mount gibt es keinen Pfad |
| `src/wristpull/workouts/push.py` (`write_device_file`) | `gio copy` als Fallback auf gvfs | wie oben |
| `src/wristpull/workouts/watch.py` (`remove_device_file`) | `gio remove` | wie oben |
| `src/wristpull/adb.py`, `importers/phone.py` | `adb`-Binary und USB-Zugriff aufs Handy | Binary und Gerätezugriff fehlen im Image |
| `src/wristpull/tui/deps.py` | `xdg-open` (Taste `o`) | kein Desktop im Container |
| `tui/app.py`, `cli.py` | `$VISUAL`/`$EDITOR`, Fallback `nano` | Editor fehlt im Image |
| TUI `copy_to_clipboard` | Textual nutzt OSC 52 | hängt vom Terminal ab, wie nativ auch |
| `contrib/systemd/*.service`, `contrib/udev/90-wristpull-garmin.rules` | `uv run wristpull …` mit `WorkingDirectory` im Repo | müssen auf `docker compose run` umgestellt werden |

Die Gerätefunktionen sind bereits über `Runner`, `roots` und `mounter` injizierbar, und die Tests nutzen
`FakeAdb` und `garmin_tree`. Daran knüpft die neue Geräteschicht an.

## 3. Zielbild

**Ein Image, zwei Services:**

```yaml
services:
  web:      # wie bisher: dauerhaft, hinter Caddy, KEIN USB-Zugriff, Config ro, genau ein Worker
  cli:      # nur bei Bedarf: docker compose run --rm cli wristpull <befehl>
    profiles: ["cli"]
    stdin_open: true
    tty: true
    network_mode: host          # Entscheidung 3: Ollama auf localhost:11434
    # USB-Zugriff nur hier (Hotplug-fähig, ohne privileged): Uhr (MTP) und Handy (ADB)
    volumes:
      - /dev/bus/usb:/dev/bus/usb
      - adb_keys:/home/wristpull/.android   # Entscheidung 1: ADB-Schlüssel persistent (Pfad an HOME anpassen)
      # plus Daten-Mount rw und Config-Mount rw (der Settings-Screen der TUI schreibt config.toml)
    device_cgroup_rules:
      - "c 189:* rmw"
```

Aufrufe:
- `docker compose run --rm cli wristpull tui`
- `docker compose run --rm cli wristpull import garmin`
- `docker compose run --rm cli wristpull pull phone`

Optional ein kleines Wrapper-Skript `contrib/bin/wristpull`, das genau das aufruft, damit sich die Befehle wie
nativ anfühlen.

**Begründung für die Trennung:** Die cgroup-Regel `c 189:*` erlaubt Zugriff auf **alle** USB-Geräte
(auch Tastatur und USB-Sticks). Das ist für ein Heim-Tool vertretbar, gehört aber nie in den
netzwerkerreichbaren `web`-Container.

**Image:** Extras `web`, `coach`, `tui` sowie `adb` und `mtp-tools` (Entscheidungen 1 und 2)
und ein kleiner Editor (`nano`, optional `micro`). Multi-Stage oder ein einziges Image, beides ist in Ordnung,
Größe im Blick behalten.

## 4. Entscheidungen (mit Philipp abgestimmt am 2026-11-07)

1. **ADB: USB-Durchreichung, der adb-Server läuft im `cli`-Container.**
   - Auf dem Host bleibt nichts installiert. Es greift dieselbe `c 189:*`-Regel wie bei der Uhr.
   - Der ADB-Schlüssel (`~/.android/adbkey`, `adbkey.pub`) liegt in einem **persistenten Volume**, sonst
     verlangt das Handy bei jedem Lauf eine neue Autorisierung.
   - Läuft auf dem Host parallel ein eigener adb-Server, kommt es zum Konflikt um das Gerät. Das erkennen und mit klarer
     Meldung abbrechen (z. B. „adb server on the host holds the phone: run `adb kill-server` on the host“).
   - **Zusätzlich** Wireless Debugging (Android 11+) über `[phone] serial = "ip:port"`. Das ist optional und nicht
     der Standard für den Morgen-Timer, weil Android es nach einem Neustart oder Netzwechsel abschaltet und der
     Port wechselt. Für den späteren Server-Umzug interessant (der Server holt das Handy direkt im WLAN ab).
   - Verworfen: der adb-Server auf dem Host (Host-Abhängigkeit bleibt, `adb -a` wäre LAN-offen) und Wireless
     Debugging als einzige Variante (zu fragil).
2. **libmtp: zuerst die Kommandozeilen-Tools** (`mtp-files`, `mtp-getfile`, `mtp-sendfile`, `mtp-delfile`
   aus `mtp-tools`), **hinter einer eigenen Schnittstelle** (`src/wristpull/mtp.py`, siehe Phase B).
   - Begründung: schnell umgesetzt, als Paket verfügbar, jeder Schritt ist von Hand nachvollziehbar.
   - Bekannte Schwächen: Jeder Aufruf öffnet das Gerät neu und liest es ein, die Ausgabe muss geparst werden.
     Deshalb die Aufrufe bündeln, wo es geht (einmal auflisten, dann gezielt per ID holen).
   - **Umkehrbar:** Wenn sich in Phase 0 oder B zeigt, dass es zu langsam oder zu fehleranfällig ist, wird
     nur die Implementierung hinter der Schnittstelle auf `ctypes` direkt auf `libmtp` getauscht. Die Aufrufer
     und `FakeMtp` bleiben unverändert.
   - **Kein FUSE-Mount im Container** (bräuchte `/dev/fuse` und `CAP_SYS_ADMIN`).
3. **`cli` nutzt `network_mode: host`.**
   - Ollama ist unter `http://localhost:11434` erreichbar, an der Ollama-Konfiguration des Hosts ändert
     sich nichts.
   - Vertretbar, weil `cli` ein kurzlebiger Befehl ohne eigenen Server ist.
   - `web` bleibt **unverändert isoliert** (Compose-Netzwerk, Caddy davor, `host-gateway` für Ollama wie in
     `docs/dashboard-concept.md` beschrieben).
   - Weiterhin tabu: `OLLAMA_HOST=0.0.0.0` und `adb -a`.

## 5. Leitplanken

- Alle Regeln aus `CLAUDE.md` gelten:
  - Gesundheitsdaten nie ins Repo.
  - Tests nie gegen Hardware oder Netzwerk.
  - Jede neue Funktion bekommt einen Test.
  - Vor jedem Commit `uv run pyflakes src tests && uv run pytest -q`.
  - Nach jeder Phase README aktualisieren und committen.
- **Rohdateien unveränderlich, Uhr nur lesen.** Einzige Schreibausnahmen bleiben Workout-Dateien in
  `GARMIN/NewFiles/` bzw. `Workouts/` (nie überschreiben) und das Entfernen von Workout-Dateien nach
  Backup. Datenordner auf der Uhr werden nie verändert. Das gilt für die neue MTP-Schicht genauso.
- Kein `privileged: true`. USB-Zugriff nur im `cli`-Service.
- `web` bleibt bei genau einem Worker, ohne USB, mit Config ro.
- Container läuft als Host-UID/GID (`user: "${UID}:${GID}"`), Dateien im Datenordner behalten ihren Besitzer.
- Die native Installation (uv, gio-Pfad, KDE-Workaround) bleibt funktionsfähig, ihre Tests bleiben grün.
- TUI und Web rufen weiter nur Bibliotheksfunktionen auf (Boundary-Tests).
- Nie `OLLAMA_HOST=0.0.0.0`, nie `adb -a`.

## 6. Umsetzung in Phasen

### Phase 0 – Spike an der echten Uhr (mit Philipp, vor jedem Umbau)

Ziel: belegen, dass libmtp im Container mit der fenix 6X Pro funktioniert. Philipp hat die Uhr früher
schon mit `jmtpfs` ausgelesen, das auf libmtp basiert. Das ist ein gutes Zeichen, aber noch kein Beleg.

- Ein Wegwerf-Container mit `mtp-tools`, `/dev/bus/usb` und der cgroup-Regel. Darin prüfen:
  - `mtp-detect`
  - Dateiliste von `GARMIN/Monitor`
  - `mtp-getfile` einer FIT-Datei, SHA-256 mit dem gvfs-Weg vergleichen
  - `mtp-sendfile` einer Test-Workout-Datei nach `NewFiles` und den Import nach dem Abziehen prüfen
- Herausfinden, wie verhindert wird, dass der Host (gvfs-Volume-Monitor, KDE kiod6/Solid) das Gerät belegt.
  Kandidaten:
  - eine udev-Regel, die für `idVendor==091e` `ID_MTP_DEVICE` leert. Muss nach den libmtp-Regeln greifen, also eine hohe Nummer bekommen.
  - als Notlösung `ExecStartPre=-/usr/bin/kquitapp6 kiod6` in der Host-Unit.

  Welche Variante greift, lässt sich nur am Gerät verifizieren.
- Rechte prüfen: Bekommt die Host-UID über `uaccess`-ACL Zugriff auf den Geräteknoten, oder braucht es eine
  udev-Regel mit `GROUP`/`MODE` plus `group_add` im Compose-File? Für das Handy (ADB) dasselbe prüfen.
- Ergebnis kurz in `docs/docker-concept.md` festhalten (neues Konzeptdokument im Stil von
  `dashboard-concept.md`). Bestätigen, dass die CLI-Tools (Entscheidung 2) mit der Uhr zuverlässig und
  schnell genug sind. Sonst gleich auf `ctypes` wechseln und Philipp kurz informieren.
- ADB im Container gleich mittesten: Handy per USB, Schlüssel im Volume, `adb devices` zeigt das Handy
  nach einem Container-Neustart ohne neue Autorisierung.

### Phase A – `cli`-Service ohne Uhr

- Image um Extra `tui`, `adb` und Editor erweitern. Service `cli` mit Profil, TTY, Daten-Mount rw,
  Config-Mount **rw**, `TERM`/`COLORTERM`/`LANG`/`EDITOR` aus dem Host durchreichen.
- ADB per USB im Container (Entscheidung 1): adb-Server im Container, persistentes `~/.android`-Volume,
  Erkennung eines konkurrierenden adb-Servers auf dem Host mit klarer Meldung. Wireless Debugging
  über `[phone] serial = "ip:port"` als dokumentierte Zusatzoption.
- Coach im `cli`-Container: `network_mode: host`, Ollama unter `http://localhost:11434` (Entscheidung 3),
  API-Keys wie bei `web` aus `.env`.
- TUI: `xdg-open` im Container sauber abfangen. Statt eines Fehlers einen Hinweis mit Pfad anzeigen (z. B. über
  einen injizierbaren Opener in `TuiDeps`, der im Container ausgewählt wird, erkannt etwa über
  `WRISTPULL_IN_CONTAINER=1`).
- Akzeptanz:
  - `docker compose run --rm cli wristpull tui` läuft auf allen Screens.
  - `pull phone` funktioniert.
  - `coach daily --dry-run` und `coach daily` funktionieren.
  - `export` und `report` landen im Host-Datenordner mit Host-Besitzer.
  - Parallel laufendes `web` sieht die neuen Daten sofort.

### Phase B – MTP-Geräteschicht

- Neues Modul, z. B. `src/wristpull/mtp.py`, mit einer schmalen Schnittstelle:
  `list(folder)`, `get(file_id, dest)`, `send(src, folder, name)`, `delete(file_id)`, `device_info()`.
  Erste Implementierung mit den `mtp-tools` (Entscheidung 2), austauschbar gegen `ctypes`. Dazu ein
  `FakeMtp` für Tests im Stil von `FakeAdb`.
- **Import:** Dateien in den konfigurierten Ordnern (`[garmin] folders`) auflisten. Neue Dateien in einen
  Staging-Ordner unter dem Datenordner laden und dann über den bestehenden Pfad
  `import_garmin(conn, config, path=staging)` importieren. Die SHA-256-Deduplizierung in `raw_files` bleibt
  die maßgebliche Prüfung. Um unnötige Downloads zu sparen, vorab nach Name und Größe gegen bereits
  archivierte Dateien filtern, falls die Metadaten das hergeben. Staging danach aufräumen.
- **Push und Remove:** `write_device_file` und `remove_device_file` bekommen eine MTP-Variante.
  Die bisherigen Regeln gelten unverändert: nie überschreiben, Remove nur nach Backup nach
  `workouts/watch-backup/`, Namensprüfung pro Woche und Workout-Limit-Warnung bleiben aktiv.
  `verify`/`watch` lesen `Workouts/` und `NewFiles/` über die MTP-Liste.
- **Auswahl der Geräteschicht:** Konfigurierbar, z. B. `[garmin] access = "auto" | "gvfs" | "mtp"`.
  `auto` wählt im Container MTP, nativ zuerst gvfs.
- `status` und `mount` im Container: Statt „gvfs-Mount“ meldet `status` „MTP-Gerät erkannt“.
  `mount --remount` erklärt im Container, dass es dort nicht nötig ist.
- Akzeptanz:
  - `docker compose run --rm cli wristpull import garmin` importiert dieselben Dateien mit identischen
    SHA-256 wie der gvfs-Weg.
  - Ein zweiter Lauf importiert nichts Neues.
  - `workout push --upcoming` → Uhr abziehen → Workout erscheint auf der Uhr.
  - `workout remove` legt vorher ein Backup an.
  - Alle bisherigen Tests bleiben grün, neue Tests mit `FakeMtp`.

### Phase C – Automatik auf dem Host

- `contrib/udev/`: Regel beim Einstecken der Uhr (wie bisher über `SYSTEMD_USER_WANTS`), dazu die Regel
  gegen die Gerätebelegung durch den Host aus Phase 0.
- `contrib/systemd/`: Container-Varianten der Units (`wristpull-import`, `wristpull-pull`, `wristpull-coach-daily`)
  mit `docker compose -f … run --rm cli wristpull -q …`, ohne `uv` und ohne Repo-Checkout. Die nativen Units
  bleiben daneben bestehen.
  - Reihenfolge wie im README „Automation“: USB-Import vor jedem Handy-Sync.
  - `workout push --upcoming` optional im Import-Service.
- Akzeptanz: Uhr einstecken → Import läuft automatisch im Container. Timer um 06:45/07:00 laufen
  ohne angemeldete Desktop-Sitzung.

## 7. Risiken

- **libmtp und die fenix 6X Pro:** Möglich sind Gerätequirks, langsame Listings großer Ordner oder Abbrüche bei vielen
  Dateien. Deshalb zuerst der Spike in Phase 0, Fallback bleibt der native gvfs-Weg.
- **Host belegt das Gerät** (gvfs, kiod6): Ohne funktionierende Host-udev-Regel bleibt der Konflikt bestehen.
  Der Container sollte dann eine klare Fehlermeldung mit Fix ausgeben, kein stilles Scheitern.
- **Rechte auf Geräteknoten:** Mit `uaccess` hängt der Zugriff an der aktiven Seat-Sitzung. Für Timer ohne
  Anmeldung (und später auf dem Server) ist eine feste udev-Regel mit Gruppe nötig.
- **Linux-only bleibt:** Unter Docker Desktop auf macOS und Windows wird USB nicht durchgereicht. Das gehört in
  die README. Ausnahme ist der reine Handy-Weg über Wireless ADB.
- **Gleichzeitige Schreiber:** WAL deckt Host-CLI, `cli` und `web` parallel ab. Lange Transaktionen beim Import
  vermeiden, `busy_timeout` prüfen (Python-Default 5 s).

## 8. Nicht Teil dieses Auftrags

- Image auf GHCR veröffentlichen und CI-Build (Folgeauftrag „Punkt 7“).
- `wristpull ship` und der Server-Umzug (Folgeauftrag „Punkt 28“). Die MTP-Schicht und die `cli`-Trennung
  sollten ihn aber nicht verbauen: Der Import muss auch auf dem Server rein aus Dateien laufen.
- `wristpull doctor` (Folgeauftrag „Punkt 8“). Die Prüfungen für die Container-Variante (USB sichtbar,
  Rechte, Host-Belegung) dort mit vorsehen.

## 9. Doku-Updates

- `README.md`: neuer Abschnitt „Running everything in Docker“ (Setup, `.env`, Aufrufe, Wrapper,
  Einschränkungen, Linux-only). Install-Abschnitt nennt beide Wege.
- `docs/docker-concept.md` (neu): Entscheidungen, Spike-Ergebnisse, Sicherheitsabwägung `c 189:*`.
- `docs/dashboard-concept.md`: Verweis auf den `cli`-Service.
- `CLAUDE.md`: MTP-Schicht, `FakeMtp`, `[garmin] access`, Container-Regeln (kein USB in `web`).
- `contrib/udev/README.md`, `contrib/systemd/README.md`: Container-Varianten.
- `.env.example`: neue Variablen (z. B. für ADB-Schlüssel und Gerätegruppe).

## 10. Definition of Done

- Alle Befehle aus der README-Befehlstabelle laufen über `docker compose run --rm cli …`.
- Der native Weg funktioniert unverändert.
- Tests grün (alte und neue, ohne Hardware), pyflakes sauber.
- An der echten Uhr bestätigt: Import (gleiche SHA-256 wie nativ), Push, Remove, Automatik beim Einstecken.
- Doku aktualisiert, pro Phase ein Commit.
