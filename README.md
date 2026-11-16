# Stadion-Echo – Discord Bundesliga-Ticker (OpenLigaDB)

Live-Ticker für 1. und 2. Bundesliga in Discord. Datenquelle: die kostenlose
[OpenLigaDB](https://www.openligadb.de) (kein API-Key).

## Was der Bot automatisch postet

| Event | Wann |
|---|---|
| 🏟 Tagesübersicht | `PRE_KICKOFF_WAKE_MINUTES` vor dem ersten Anstoß des Tages |
| 🟢 Anpfiff (mit Wappen-Bild) | zur Anstoßzeit (OpenLigaDB hat keinen Live-Status → zeitbasiert) |
| ⚽ Tor | sobald Tor oder Spielstand in der API auftaucht; Torschütze wird per **Edit nachgetragen** |
| ⚠️ Korrektur | wenn ein Tor wieder verschwindet (VAR/Datenfehler) |
| ⏸ Halbzeit | sobald das Halbzeitergebnis eingetragen ist |
| 🏁 Abpfiff | mit Halbzeitstand, Zuschauern und allen Torschützen |
| 📡 Live-Konferenz | **eine** Nachricht pro Tag, die laufend editiert wird (alle Spielstände) |
| 📊 Tabelle | ein paar Minuten nach dem letzten Spiel des Tages je Liga |

Alles ist idempotent (Tabelle `posted_events`): Neustarts erzeugen keine Duplikate,
und ein Event gilt erst als gepostet, wenn Discord die Nachricht angenommen hat.
Startet der Bot erst nach einem Spiel, wird nur der Abpfiff nachgereicht statt alles zu spammen.

## Commands

User: `/hilfe` `/heute` `/live` `/upcoming` `/spieltag` `/spiel <verein>` `/verein <name>` `/tabelle` `/torjaeger`
(Vereinsnamen mit Autocomplete; Aliase wie `bvb`, `effzeh`, `hsv`, `s04` funktionieren.)

Admin (IDs aus `DISCORD_ADMIN_USER_IDS` **oder** Server-Admins):
`/status` (Ticker-Zustand, Rechte-Check, nächstes Spiel) · `/sync` · `/reload` ·
`/test_anpfiff` `/test_tor` `/test_halbzeit` `/test_abpfiff` `/test_opener` `/test_konferenz` `/test_tabelle`
– die Tests posten mit echtem Layout in den Ticker-Channel und melden fehlende Rechte.

## Update auf diese Version

```bash
sudo systemctl stop football_bot
cd /home/zer0/football_bot_discord
# alten Stand sichern, dann den Inhalt des Archivs drüberkopieren (.env und venv bleiben)
cp -r app app.bak
# … Dateien kopieren …
source venv/bin/activate && pip install -r requirements.txt
sudo cp football_bot.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl start football_bot && sudo journalctl -u football_bot -f
```

- **Keine DB-Migration nötig** – das Schema ist kompatibel. Fehlende Tabellen und die Ligen
  `bl1`/`bl2` legt der Bot beim Start selbst an.
- `app/scheduler.py`, `app/live_poller.py`, `app/repositories/event_templates.py` und
  `app/config/message_templates.json` werden nicht mehr gebraucht und können gelöscht werden.
- `.env`: neue optionale Schlüssel siehe `.env.example`. `SEASON=0` = automatisch.
  `DISCORD_WELCOME_CHANNEL_ID` gesetzt ⇒ im Developer-Portal **Server Members Intent** aktivieren
  (sonst `=0` setzen).
- Bot-Rechte im Ticker-Channel: Kanal ansehen, Nachrichten senden, Links einbetten,
  Dateien anhängen, Nachrichtenverlauf lesen. `/status` prüft das.

## Neuinstallation

```bash
sudo apt install libcairo2 fonts-dejavu-core   # für die Wappen-Bilder
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
cp .env.example .env   # ausfüllen
mysql -u root -p -e "CREATE DATABASE football_bot CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
  CREATE USER 'football_bot'@'localhost' IDENTIFIED BY '…';
  GRANT ALL PRIVILEGES ON football_bot.* TO 'football_bot'@'localhost';"
python -m app.main
```

Bot einladen mit den Scopes `bot` **und** `applications.commands`.

## Tests

```bash
pip install pytest && python -m pytest tests
```
Simuliert einen ganzen Spieltag (Opener → Anpfiff → Tor ohne/mit Schütze → Halbzeit →
VAR-Korrektur → Abpfiff → Tabelle → Neustart ohne Duplikate) ohne DB und ohne Discord.

## Architektur

```
app/main.py            Start, Logging, Signale
app/discord_bot.py     Bot, Command-Sync (setup_hook), startet den Ticker
app/ticker.py          Engine: wann pollen, welche Events posten (ersetzt scheduler + live_poller)
app/matchdata.py       reines Parsing des OpenLigaDB-JSON (Spielstand, Tore, Seite, Halbzeit)
app/formatters.py      alle Embeds
app/publisher.py       Senden/Editieren + Rechte-Check
app/sync.py            API → DB (Season, Spieltag, Tabelle, Aliase)
app/openligadb_client.py  HTTP mit Retry
app/commands.py        Slash-Commands
```

**Polling-Strategie:** Während Spielen fragt der Bot pro Liga+Spieltag nur
`/getlastchangedate/{liga}/{saison}/{spieltag}` ab (winzige Antwort). Nur wenn sich der
Zeitstempel ändert – spätestens alle 2 Minuten – wird der ganze Spieltag in **einem** Request
geladen. Außerhalb der Spielzeiten schläft der Bot bis kurz vor den nächsten Anstoß und
synchronisiert alle 12 h die Saison (Anstoßzeiten werden oft erst kurzfristig festgelegt).
