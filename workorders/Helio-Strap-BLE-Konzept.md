# Konzept: Helio Strap direkt per Bluetooth (ohne Gadgetbridge)

Stand: 2026-11-07, Entwurf auf Basis der Gadgetbridge-Quellen (Codeberg, Stand heute) und des
aktuellen wristpull-Datenbestands. Noch kein Auftrag, noch keine Zeile Code. Repository-Stand `bfe1ff3`.

## 1. Ziel

wristpull holt die Daten des Amazfit Helio Strap selbst über Bluetooth Low Energy (BLE) ab, zuerst
vom Laptop aus, später von einer eigenständigen Box (Raspberry Pi). Zusammen mit dem USB-Import
der Garmin-Uhr entfällt das Handy mit Gadgetbridge komplett. Die Box wäre dann der Ort, an dem
alle Gerätedaten eingehen, liegen und ausgewertet werden (Dashboard, Coach). Das ist die Vision
„Fitness-Cube“.

Was dafür nachgebaut werden muss, ist genau das, was Gadgetbridge für Zepp-OS-Geräte tut: BLE-
Verbindung, Authentifizierung mit dem Geräteschlüssel, verschlüsselter Kanal, Abholen der
gespeicherten Messwerte. Es ist technisch möglich, die Protokollfakten stehen unten.

## 2. Befund: Wie Gadgetbridge mit dem Strap spricht

Der Helio Strap ist in Gadgetbridge ein Zepp-OS-Gerät (`AmazfitHelioStrapCoordinator`, Name
`Amazfit Helio Strap`, Firmware auf dem Band laut letztem Export `3.25.0.3`, MAC `F6:D9:7B:6A:EC:D3`,
nur BLE, kein Bluetooth Classic). Die Verbindung läuft in drei Schichten.

### 2.1 GATT

| Zweck | UUID |
|---|---|
| Haupt-Service | `0000fee0-0000-1000-8000-00805f9b34fb` |
| Chunked-Transfer schreiben | `00000016-0000-3512-2118-0009af100700` |
| Chunked-Transfer lesen (Notify) | `00000017-0000-3512-2118-0009af100700` |
| Activity-Control (Fallback, Write + Notify) | `00000004-0000-3512-2118-0009af100700` |
| Activity-Data (Notify, unverschlüsselt) | `00000005-0000-3512-2118-0009af100700` |
| Current Time (Fallback) | `00002a2b-…` (11 Byte, eigenes Format) |
| Device Information | `0000180a-…` (Firmware, Hardware, PnP) |

MTU: 23 zum Start, nach der Authentifizierung optional 247 anfordern. Kein BLE-Bonding auf OS-Ebene
nötig (`BONDING_STYLE_REQUIRE_KEY`), die Sicherheit liegt im Auth-Key.

### 2.2 Framing (Huami-2021-Chunked-Protokoll)

Jede Nachricht geht an einen 16-Bit-Endpunkt und wird in Chunks à MTU geschrieben:

```
Byte 0      0x03
Byte 1      Flags: 0x01 erster Chunk, 0x02 letzter Chunk, 0x04 braucht Ack, 0x08 verschlüsselt
Byte 2      0x00 (Extended-Flag-Byte, bei Zepp OS immer vorhanden)
Byte 3      Handle (läuft pro Nachricht hoch)
Byte 4      Chunk-Zähler
nur im ersten Chunk:  Länge u32 LE, Endpunkt u16 LE
Rest        Nutzdaten
```

Verschlüsselte Nachrichten: `Nutzdaten + Sequenznummer u32 LE + CRC32(Nutzdaten+Seq) u32 LE`, auf
16 Byte aufgefüllt, AES-128-ECB mit `Session-Key XOR Handle` (byteweise). Die Sequenznummer startet
mit dem Wert aus dem Schlüsseltausch und zählt pro Nachricht hoch. Antworten kommen gleich gerahmt
auf der Lese-Characteristic; ein Chunk mit Flag `0x04` wird mit einem 0x04-Paket auf der Schreib-
Characteristic quittiert. Der Decoder sammelt Chunks nach Handle, entschlüsselt am Ende und
schneidet auf die angekündigte Länge.

### 2.3 Authentifizierung (Endpunkt `0x0082`, unverschlüsselt)

1. Client erzeugt 24 Byte Zufall als privaten Schlüssel, berechnet den öffentlichen Punkt (48 Byte)
   auf der Kurve **NIST B-163** (binäres Feld, Gadgetbridge trägt dafür eine ~500-Zeilen-Portierung
   von tiny-ECDH-c) und sendet `04 02 00 02 + pub(48)`.
2. Band antwortet `10 04 01 + random(16) + pub_band(48)`.
3. Client bildet das gemeinsame Geheimnis `shared(48)`.
   `seq = shared[0:4]` (u32 LE), `session_key[i] = shared[8+i] XOR auth_key[i]` für i in 0..15.
4. Client sendet `05 + AES(random, auth_key) + AES(random, session_key)` (ECB, 16 Byte).
5. Band antwortet `10 05 01` (ok) oder `10 05 25` (falscher Schlüssel).

Der `auth_key` sind 16 Byte (32 Hex-Zeichen, in Gadgetbridge als `0x…` eingetragen). Er ist pro
Band fest und gilt für jeden Host, der ihn kennt. Gadgetbridge und wristpull können denselben
Schlüssel benutzen, nur nicht gleichzeitig verbunden sein.

### 2.4 Initialisierung nach der Authentifizierung

- Endpunkt `0x0000`, Befehl `03`: Liste der Services; Antwort `04 + n(u16) + n × (endpoint u16, encrypted u8)`.
  Daraus weiß der Client, welche Endpunkte verschlüsselt sein müssen.
- Zeit setzen (Endpunkt `0x0047`, `05`, oder die Current-Time-Characteristic), Geräteinfo abfragen.
- Endpunkt `0x0015` beantwortet Pings (`03` → `04`) und verhandelt die MTU. Sonst ist für das reine
  Abholen nichts nötig; Benachrichtigungen, Wetter, Alarme usw. sind getrennte Services und entfallen.

### 2.5 Abholen der Messwerte (Endpunkt `0x004b`, sonst Characteristic `0004`)

```
Client:  01 <typ> <jahr u16 LE> <monat> <tag> <stunde> <minute> <sekunde> <tz in Viertelstunden>
Band:    10 01 01 <anzahl pakete u32> <zeitstempel erstes sample (7–8 Byte)>     (anzahl 0 = nichts)
Client:  02
Band:    Daten als Notifications auf 0005: [zähler u8][nutzdaten …], Zähler lückenlos
Band:    10 02 01 [crc32 u32]
Client:  03 09   (Daten auf dem Band behalten)  bzw.  03 01   (Band darf löschen)
```

Pro Datentyp merkt sich der Client den Zeitstempel des letzten Samples und fragt beim nächsten Mal
ab dort (Gadgetbridge: beim ersten Mal 100 Tage zurück, höchstens zehn Runden pro Sync).

| Typ | Code | Format pro Datensatz | wristpull-Ziel | Parser vorhanden? |
|---|---|---|---|---|
| Aktivität | `0x01` | 8 Byte pro Minute ab Startzeit: kind, intensity, steps, hr, ?, sleep, deep, rem | `steps`, `heart_rate`, Minuten-Schlafflags | ja (`gadgetbridge.py`, HUAMI_EXTENDED_ACTIVITY_SAMPLE) |
| Stress automatisch | `0x13` | 1 Byte pro Minute, `0xff` = kein Wert | `stress` | ja |
| SpO2 | `0x25` | Version-Byte 2, dann 65 Byte: ts u32, spo2 (Bit 7 = automatisch), 60 Byte unbekannt | `spo2` | ja |
| Ruhepuls | `0x3a` | 6 Byte: ts u32, tz, bpm | `resting_hr` | ja |
| HRV | `0x49` | 6 Byte: ts u32, ?, hrv ms | `hrv_values` | ja |
| Atemfrequenz Schlaf | `0x38` | 8 Byte: ts u32, tz, rate, ?, ? | `respiration` | ja |
| Schlaf-Session | `0x48` | 594-Byte-Blobs, ts u32 am Anfang | `sleep`, `sleep_stages` | ja (`decode_sleep_session`) |
| PAI | `0x0d` | eigenes Format | `metrics` | nein (bisher nicht importiert) |
| Puls max/manuell | `0x3d`/`0x02` | 6 Byte | `metrics` | nein |
| Workouts | `0x05` + `0x06` | Summary-Binärformat + Detail-Records | `activities` | nein, Gadgetbridge-Parser müsste portiert werden |
| Statistik | `0x2c` | unbekannt | – | – |

Die Formate sind dieselben, die heute über `pull phone` aus den Gadgetbridge-Tabellen kommen; die
Decoder in `gadgetbridge.py` lassen sich fast unverändert auf die BLE-Puffer anwenden. Im letzten
Export vom 2026-11-06 hält Gadgetbridge für den Strap: 8 856 Minutensamples, 742 Stresswerte,
468 SpO2, 2 005 HRV, 2 612 Atemfrequenzen, 10 Schlaf-Sessions, 6 Ruhepulse, 20 PAI, 5 Maximalpulse.

### 2.6 Woher der Auth-Key kommt

Der Schlüssel entsteht beim ersten Koppeln mit der Zepp-App und liegt auf Huamis Servern sowie im
Band. Wege, ihn zu bekommen:

1. **Aus Gadgetbridge** (einfachster Weg, er ist dort schon eingetragen): Gadgetbridge → Einstellungen →
   Backup/Export als ZIP. Darin liegen die Geräte-Einstellungen als JSON mit dem Feld `authkey`. Die
   ZIP per `adb pull` holen, Wert ablesen.
2. **huami-token** (Python-Skript, Zepp-Konto nötig): `python3 huami_token.py --method amazfit --email … --password … --bt_keys`.
3. Entkoppeln in der Zepp-App macht den Schlüssel ungültig; dann wieder koppeln und neu holen.

Ablage in wristpull: `<data_dir>/strap/authkey` (Datei, 0600), nie in `config.toml` im Repo, nie im
Git. Für ein Produkt ist das die größte Hürde: ohne Zepp-Konto und Zepp-App gibt es keinen Schlüssel
(siehe Risiken).

## 3. Rahmenbedingungen

- **Ein Host zur Zeit.** Ein BLE-Peripheriegerät hält genau eine Verbindung; solange Gadgetbridge
  verbunden ist, ist das Band für den Laptop unsichtbar (es sendet dann keine Advertisements).
  Übergang: Band in Gadgetbridge trennen (oder dort entfernen), dann vom Laptop verbinden. Endzustand:
  nur noch wristpull kennt das Band. Gadgetbridges Auto-Reconnect ist dabei der Störenfried.
- **Laptop:** Intel 9460/9560-Bluetooth, BlueZ 5.85, `hci0` läuft. `bleak` (Python-BLE über BlueZ
  D-Bus) ist nicht installiert → neue optionale Extra-Gruppe `ble` mit `bleak` und `cryptography`
  (AES-ECB; die Stdlib hat kein AES). ECDH B-163 wird als reines Python portiert (Feldarithmetik auf
  Python-Ints, kein weiteres Paket).
- **Docker:** BLE im Container braucht `network_mode: host` (hat `cli` schon) plus den D-Bus-Socket
  `/run/dbus/system_bus_socket` als Mount und die BlueZ-Policy für den Nutzer; nativ mit `uv` geht es
  ohne das. Auf dem Raspberry Pi gilt dasselbe (BlueZ an Bord, arm64-Image).
- **Nie löschen auf dem Gerät.** wristpull quittiert mit `03 09` (Daten behalten) und führt die
  „seit“-Zeitstempel selbst. Ob das Band dann alte Daten irgendwann selbst verwirft und wie lange es
  Daten vorhält, zeigt der Spike. Gadgetbridge löscht standardmäßig nach dem Abholen.
- **Rohdaten bleiben unveränderlich.** Jeder abgeholte Puffer wird als Datei archiviert
  (`raw/zepp_ble/<jahr>/<typ>/<start>_<typ>.bin`, kleiner Kopf mit Typ, Startzeit, Zeitzone), SHA-256-
  Dedup in `raw_files`, Kinds `zepp_activity`, `zepp_stress`, `zepp_spo2`, … Damit funktioniert
  `reparse` nach Parser-Änderungen wie bei den FIT-Dateien.
- **Lizenz.** Gadgetbridge ist AGPL-3.0. Eine Neuimplementierung des Protokolls nach Lektüre der
  Quellen ist für den Eigengebrauch unkritisch; für ein verkauftes Produkt ist das vorher zu klären
  (eigene Implementierung mit dokumentierter Protokollbeschreibung statt Code-Übernahme; die ECDH-
  Routine stammt ursprünglich aus tiny-ECDH-c, Unlicense).

## 4. Entwurf in wristpull

Paket `src/wristpull/zeppos/`, nach dem Muster von `mtp.py`/`adb.py`: Protokoll ohne I/O, Transport
austauschbar, alles testbar ohne Band.

| Modul | Inhalt |
|---|---|
| `ecdh_b163.py` | Kurve B-163: `generate_public(private) -> bytes`, `shared_secret(private, remote_public) -> bytes`; Testvektoren aus dem Spike |
| `crypto.py` | `session_key(shared, auth_key)`, `aes_ecb_encrypt/decrypt`, `auth_response(random, auth_key, session_key)` |
| `chunked.py` | `encode(endpoint, payload, *, handle, seq, key, mtu) -> list[bytes]`, `Decoder.feed(chunk) -> Message | None`, `ack(handle, count)`; reine Funktionen |
| `protocol.py` | Zustandsautomat ohne I/O (sans-IO): `Session.start()`, `Session.receive(characteristic, data) -> list[Action]`, Aktionen = `Write(uuid, bytes)`, `Notify(uuid, on)`, `Done(...)`; deckt Auth, Services, Zeit, Fetch |
| `transport.py` | `Transport`-Protocol (`connect`, `write`, `start_notify`, `disconnect`); `BleakTransport` dahinter; `FakeTransport` in `tests/fake_ble.py` spielt aufgezeichnete Antworten ab |
| `fetch.py` | Datentypen (`FetchType`), Zeitcodierung, „seit“-Logik, Runden-Begrenzung, Puffer → `RawBuffer(type, start, tz, data)` |
| `parsers.py` | `RawBuffer` → Zeilen; ruft die Decoder aus `gadgetbridge.py` auf (Sleep-Session, Stage-Flags, SpO2, …) |
| `importers/strap.py` | `pull_strap(conn, config, *, transport_factory, log, stop, dry_run)`: verbinden, authentifizieren, Typen der Reihe nach holen, archivieren, parsen, `rebuild_daily_summary(source="zepp_ble")` |

Weitere Punkte:

- **Quelle** `zepp_ble`; Merge-Priorität für den Strap `("zepp_ble", "gadgetbridge_zepp")` (aus
  `STRAP_SOURCE` wird eine Liste wie `GARMIN_PRIORITY`; Dedup nach Minute bzw. Startminute wie heute).
  Während der Übergangszeit liefern beide Wege dasselbe Band, die Ansicht bleibt eindeutig.
- **Konfiguration** `[strap]`: `address`, `adapter = "hci0"`, `authkey_file`, `access = auto | gadgetbridge | ble`,
  `keep_on_device = true`. Sync-Stand pro Typ in einer Tabelle `strap_sync` (SQL in einem `store.py`).
- **CLI:** `wristpull strap scan` (Advertisements, Name, RSSI), `wristpull strap pair` (Auth-Key
  ablegen, Probe-Verbindung), `wristpull pull strap` (Abholen), `status` zeigt Band/letzten Sync. TUI und
  Web bekommen den Job wie `pull phone`; `wristpull-morning` ersetzt Schritt 2 durch „Strap in Reichweite?“.
- **Tests:** `tests/fake_ble.py` + Fixtures aus dem Spike (`tests/fixtures/zeppos/*.hex`): Auth-Dialog,
  Service-Liste, ein Fetch je Typ; Crypto-Tests mit festen Schlüsseln; conftest blockt `bleak` wie
  heute `Adb` und die MTP-Tools. Boundary-Tests (TUI/Web ohne Protokoll) bleiben.

## 5. Phasen

| Phase | Inhalt | Ergebnis |
|---|---|---|
| 0 Spike (1 Session, Band in Reichweite, Gadgetbridge getrennt) | Auth-Key aus dem Gadgetbridge-Backup; Wegwerf-Skript im Scratchpad: `bleak`-Scan, verbinden, B-163-Port, Auth, Service-Liste, Zeit, **ein** Fetch (Stress seit gestern), Hexdumps sichern; Vergleich mit den Werten im Gadgetbridge-Export; Verhalten von `03 09` (behalten) prüfen | Fixtures, Protokoll bestätigt, Abbruchkriterien geprüft |
| 1 Bibliothek + `pull strap` (2–3 Sessions) | Module aus §4 für Aktivität, Stress, SpO2, HRV, Ruhepuls, Atmung, Schlaf-Sessions; Rohdatei-Archiv, Reparse, Merge, Status, TUI/Web-Job, Morgen-Skript, README, `CLAUDE.md` | Strap ohne Handy, Werte identisch zu Gadgetbridge |
| 2 Rest (1–2 Sessions) | Workouts (`0x05`/`0x06`, Parser-Port), PAI, Puls max/manuell; optional Echtzeit-Puls | Parität mit `pull phone` |
| 3 Box (eigener Work-Order) | Raspberry Pi: arm64-Image, Docker mit D-Bus-Mount, Scan-Schleife „Band in Reichweite → abholen“, Uhr per USB wie heute, Web-Dashboard im LAN, Backup; Produktfragen aus §6 | Fitness-Cube |

Abbruchkriterien für den Spike: Band verweigert den Schlüssel trotz korrekter Werte (`10 05 25`),
B-163-Port liefert kein Geheimnis, das das Band akzeptiert, oder BlueZ hält die Verbindung nicht lange
genug für einen Fetch. Dann bleibt Gadgetbridge der Partner und der Laptop zieht per Wireless ADB.

## 6. Risiken und offene Fragen

1. **ECDH-Port.** B-163 ist nicht in gängigen Python-Bibliotheken; der Port ist die einzige Stelle,
   an der ein Rechenfehler alles blockiert. Im Spike zuerst gegen das Band verifizieren (Auth ok =
   Mathematik ok).
2. **Datenvorhaltung im Band** bei „behalten“-Quittung: unbekannt; messen. Wenn das Band nur wenige
   Tage puffert, muss die Box oft genug in Reichweite sein (wie heute das Handy).
3. **BlueZ-Stabilität** (Verbindungsabbrüche, MTU, Notify-Verlust) – Gadgetbridge hat dafür Jahre an
   Workarounds; mit Wiederholung pro Typ und Zähler-Prüfung ist das beherrschbar.
4. **Firmware-Updates** des Bands kommen nur über die Zepp-App; ohne sie bleibt das Band auf `3.25.0.3`.
   Wahrscheinlich egal, aber Protokolländerungen durch Updates fallen damit auch weg.
5. **Produkt:** Der Auth-Key setzt ein Zepp-Konto und die Zepp-App voraus (einmal pro Band). Ein
   Käufer müsste diesen Schritt selbst gehen; ein Pairing ohne Zepp ist nicht bekannt. Dazu die
   Lizenzfrage aus §3. Beides vor Phase 3 klären.
6. **Zeitzonen:** Die Fetch-Anfrage und mehrere Antwortformate tragen den Offset in Viertelstunden;
   wristpull speichert UTC plus `tz`/`local_date`, der Umgang ist wie bei Gadgetbridge-Samples.

## 7. Aufwand

Spike eine Session, Phase 1 zwei bis drei, Phase 2 ein bis zwei. Phase 3 ist ein eigener Work-Order,
der erst Sinn ergibt, wenn Phase 1 am Laptop stabil läuft.
