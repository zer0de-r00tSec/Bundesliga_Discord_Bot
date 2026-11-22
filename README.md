# Stadion-Echo – Discord Bundesliga-Ticker (OpenLigaDB)

Live-Ticker für 1. und 2. Bundesliga in Discord. Datenquelle: die kostenlose
[OpenLigaDB](https://www.openligadb.de) (kein API-Key).

## Channel-Aufbau (ab v4)

```
📁 Live-Ticker            (deine Kategorie, Bot legt hier ggf. Pokal/Frauen an)
   #die-konferenz         DISCORD_TICKER_CHANNEL_ID – alle Spiele 1. + 2. Liga, Live-Konferenz, Tabellen
   #dfb-pokal             automatisch, Konferenz DFB-Pokal
   #frauen-bundesliga     automatisch, Konferenz Frauen-Bundesliga + DFB-Pokal Frauen
📁 Bundesliga 1           automatisch, jeden Spieltag neu
   #🔴dortmund-vs-köln    Vorschau, Anpfiff, Tore, Halbzeit, Endstand – nur dieses Spiel
   #bayern-vs-leverkusen
📁 Bundesliga 2
   #✅magdeburg-vs-fürth
```

- `MATCH_CHANNEL_PREPARE_HOURS` (Standard 24) vor dem ersten Anstoß eines Spieltags legt der Bot die
  Spiel-Channels an und löscht die des alten, beendeten Spieltags. Verlegte Spiele behalten ihren Channel,
  bis sie gespielt sind.
- 🔴 = läuft, ✅ = beendet (Discord erlaubt nur 2 Umbenennungen pro 10 Min. – reicht dafür).
- Discord-Textchannels können keine Großbuchstaben/Leerzeichen → `dortmund-vs-köln`. Der volle Name
  steht im Channel-Thema. Trenner: `MATCH_CHANNEL_SEPARATOR`.
- Rechte der Spiel-Channels kommen von der Kategorie: einmal in „Bundesliga 1/2" `@everyone` →
  *Nachrichten senden* aus, dann ist jeder neue Channel automatisch schreibgeschützt.
- **Karten (Gelb/Gelb-Rot/Rot) gibt es nicht:** OpenLigaDB liefert pro Spiel nur Ergebnisse und Tore.
- Pokal: Verlängerung und Elfmeterschießen werden erkannt („2:2 i.E.", Elfmeterschießen 4:3).

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
`/kanaele` (Spiel-/Konferenz-Channels jetzt anlegen) ·
`/test_anpfiff` `/test_tor` `/test_halbzeit` `/test_abpfiff` `/test_opener` `/test_konferenz` `/test_tabelle`
– die Tests posten mit echtem Layout in den Ticker-Channel und melden fehlende Rechte.

## Update auf v4 (Spiel-Channels, Pokal, Frauen)

1. **Bot-Recht:** Servereinstellungen → Rollen → Rolle des Bots → **Kanäle verwalten** an
   (zusätzlich wie bisher: Kanäle ansehen, Nachrichten senden, Links einbetten, Dateien anhängen,
   Nachrichtenverlauf lesen). Die Bot-Rolle muss in der Rollenliste über `@everyone` stehen.
2. `sudo systemctl stop football_bot`
3. Dateien drüberkopieren (`.env`, `venv`, `log/` bleiben), z.B. `cp -r app app.bak` vorher.
4. `.env` ergänzen (alles optional, Standardwerte siehe `.env.example`):
   ```
   LEAGUES=bl1,bl2,dfb,ffb1,dfbf
   MATCH_CHANNEL_LEAGUES=bl1,bl2
   MATCH_CATEGORY_NAMES=bl1:Bundesliga 1,bl2:Bundesliga 2
   CONFERENCE_CHANNEL_NAMES=dfb:dfb-pokal,ffb1:frauen-bundesliga,dfbf:frauen-bundesliga
   ```
   Dein bisheriger `DISCORD_TICKER_CHANNEL_ID` ist ab jetzt „Die Konferenz" (gern umbenennen).
5. `sudo systemctl start football_bot && sudo journalctl -u football_bot -f`
   Neue Tabelle `match_channels` legt der Bot selbst an.
6. Im Discord `/kanaele` (Admin) → legt Pokal-/Frauen-Konferenz und die Channels des nächsten
   Spieltags sofort an, statt auf das 24-h-Fenster zu warten. `/status` zeigt, ob das Recht passt.
7. Optional: Kategorien „Bundesliga 1/2" auf schreibgeschützt stellen (siehe oben).
8. Testlauf: `/test_spiel liga:1. Bundesliga` (dann 2. BL, Frauen, Pokal) spielt das letzte beendete Spiel
   komplett durch – Konferenz + temporärer Channel `🧪test-…` in der Liga-Kategorie. Danach
   `/test_aufraeumen` → alle Test-Nachrichten und Test-Channels sind wieder weg
   (nur bis zum nächsten Bot-Neustart, die Liste liegt im Speicher).

## Sprüche

`app/assets/sprueche.yaml` – über 250 zufällige Sprüche für Tagesstart, Vorschau, Anpfiff, Tor, Ausgleich,
Elfmeter, Eigentor, späte Tore, Halbzeit, Abpfiff (Sieg/Remis/0:0/Torfestival), Pokal, VAR-Korrektur
und Willkommen. Einfach Zeilen ergänzen – der Bot lädt die Datei bei jeder Änderung selbst neu.
Platzhalter: `{team} {gegner} {heim} {gast} {sieger} {verlierer} {stand} {name}`.
`/reload` zeigt, wie viele Sprüche pro Kategorie geladen sind. Braucht `pip install -r requirements.txt` (PyYAML).

## Channels leeren

`/leeren ziel:alle Ticker-Channels bestaetigen:True` löscht **alle** Nachrichten in den Konferenz-
und Spiel-Channels (`nur_bot:True` = nur Bot-Nachrichten). `ziel:dieser Channel` nur im aktuellen.
Bot braucht dort **Nachrichten verwalten** + **Nachrichtenverlauf lesen**. Nachrichten älter als
14 Tage löscht Discord nur einzeln – dann dauert es länger. Bereits gemeldete Spiele werden danach
nicht erneut gepostet.

## Auto-Rolle für neue Mitglieder

`DISCORD_AUTO_ROLE_ID=<Rollen-ID>` (Rechtsklick auf die Rolle → „Rollen-ID kopieren").
Bot braucht **Rollen verwalten**, seine Rolle muss in der Liste **über** der Auto-Rolle stehen, und im
Developer-Portal → Bot → *Server Members Intent* muss an sein. Mit Regel-Bestätigung (Mitgliedschafts-
Screening) gibt es die Rolle erst nach dem Akzeptieren.

## Update auf v3

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
app/ticker.py          Engine: wann pollen, welche Events posten (Konferenz + Spiel-Channel)
app/channels.py        Kategorien/Spiel-Channels je Spieltag anlegen, umbenennen, aufräumen
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
