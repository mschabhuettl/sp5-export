# Vom SP5-Export zum fertigen Plan

Diese Anleitung beschreibt die ganze Kette in der Reihenfolge, in der man sie durchläuft: einrichten, aus SP5
exportieren, die Meldungen und den Bericht lesen, Stammdaten in Dispofold bereinigen und als Korrekturdatei
zurückgeben, importieren, planen, prüfen, abschließen und Daten für eine Fehlersuche weitergeben. Die Einzelheiten
jeder Option stehen im [README](../README.md), die der Oberfläche von Dispofold in dessen Anleitung
(`docs/anleitung.md` im Dispofold-Repository).

Alle Beispiele sind erfunden. Die Bereiche und Schichtkürzel (`AN`, `DR`, `TV`, `LF`, `TS`, `A1`, `R1` usw.) sind
die aus `mapping.example.json`, die Personen-Ids (`E1001` usw.) die eines synthetischen SP5-Ordners. Jeder Befehl
dieser Anleitung lässt sich mit einem solchen Ordner ausprobieren (siehe
[Ausprobieren ohne echte Daten](#ausprobieren-ohne-echte-daten)).

## 1. Einrichten

**Exportprogramm.** Das Repository klonen (die Adresse steht auf der Seite des Repositorys):

```sh
git clone <Adresse des Repositorys> sp5-export
```

Gebraucht wird nur Python 3.10 oder neuer (`python3 --version`, unter Windows `py -3 --version`). Weitere Pakete
braucht der Export nicht; `requirements-dev.txt` ist nur für Tests und Prüfungen (siehe README, Entwicklung).

**Dispofold.** Entweder ein Programm für einen Arbeitsplatz aus den Release-Dateien von Dispofold (Windows-Setup,
Windows portabel, Linux-Pakete) oder der Docker-Server: die Release-Dateien in einen Ordner kopieren, in `.env`
`TAG` auf die Version setzen und `docker compose up -d` (Einzelheiten in `docs/betrieb.md` von Dispofold).

**Arbeitsordner.** Zuordnung, Korrekturdatei, Datensätze, Berichte und Protokolle gehören nicht ins Repository.
Am einfachsten liegen sie in einem eigenen Ordner neben dem Repository:

```
sp5-export/            das Repository
arbeit/
  mapping.json         die Zuordnung des Betriebs
  korrekturen.json     die Korrekturdatei (ab der zweiten Runde)
  dataset-2027-01.json Datensatz, Bericht und Protokoll je Export
  bericht-2027-01.csv
  export-2027-01.log
```

Liegen sie doch im Repository-Ordner, schließt `.gitignore` alle `*.json` außer dem Beispiel und alle `*.csv` aus;
die Protokolle (`*.log`) aber nicht.

**Zuordnung.** Die Zuordnung (`--mapping arbeit/mapping.json` oder, aus dem Arbeitsordner heraus,
`--mapping mapping.json`) sagt, welche SP5-Schicht zu welchem Bereich gehört und welche SP5-Gruppe welche Bereiche
gibt. Man beginnt mit einer Kopie von `mapping.example.json` und passt sie an den eigenen Bestand an. Aufbau,
gekürzt und mit den erfundenen Kürzeln des Beispiels:

```json
{
  "areas": [{"id": "AN", "name": "Notrufannahme", "color": "#B45309"},
            {"id": "DR", "name": "Disposition Rettung", "color": "#15803D"}],
  "shifts": [{"match": "A*", "areaId": "AN"}, {"match": "R*", "areaId": "DR"}],
  "ignoreShifts": ["X1-*", "X2"],
  "groups": [{"match": "Team Annahme", "areaIds": ["AN"]},
             {"match": "Team Disposition", "areaIds": ["DR"]}],
  "ignoreGroups": ["Schicht *", "Gesamt"],
  "excludeGroups": ["Ehrenamt*", "Verwaltung*"],
  "compensatoryRestAbsences": ["ER"],
  "ignoreAbsences": []
}
```

- `areas`: die Bereiche, wie sie in Dispofold erscheinen.
- `shifts`: SP5-Schichtkürzel (Muster) zu Bereich; die erste passende Regel gilt. `ignoreShifts`: Schichten, die
  bewusst nicht geplant werden.
- `groups`: SP5-Gruppennamen (Muster) zu Bereichen, also die Qualifikation. `ignoreGroups`: Gruppen ohne Bedeutung
  dafür. `excludeGroups`: wer in einer dieser Gruppen ist, wird nicht übernommen.
- `compensatoryRestAbsences`, `ignoreAbsences`: Abwesenheitsarten, die Ersatzruhe sind oder wegfallen.

Alle Schlüssel und Muster stehen im README unter Zuordnungsdatei. Die Zuordnung wird vor dem Lesen des SP5-Ordners
ganz geprüft; Fehler nennen Liste und Eintragsnummer.

**Korrekturdatei.** `korrekturen.json` gibt es erst, wenn in Dispofold etwas korrigiert und als Korrekturdatei
gespeichert wurde (siehe [3. Bereinigen](#3-bereinigen-korrekturen-in-dispofold)). Sie liegt im Arbeitsordner neben
der Zuordnung und kommt bei jedem weiteren Export mit `--corrections` dazu.

## 2. Exportieren

Aus dem Arbeitsordner, für Jänner 2027, mit allen Optionen, die im laufenden Betrieb verwendet werden:

```sh
cd arbeit
python3 ../sp5-export/sp5_export.py --data <SP5-Ordner> --month 2027-01 \
  --mapping mapping.json --corrections korrekturen.json \
  --report bericht-2027-01.csv --hidden-shifts-months 3 --recent-months 3 \
  --out dataset-2027-01.json 2> export-2027-01.log
```

Unter Windows dasselbe in einer Zeile mit `py -3 ..\sp5-export\sp5_export.py` und dem SP5-Ordner als
Laufwerksbuchstabe oder UNC-Pfad in Anführungszeichen; `2> export-2027-01.log` leitet in der
Eingabeaufforderung die Meldungen ebenso in die Protokolldatei um.

- `--data`: der SP5-Datenordner (der Ordner mit `5EMPL.DBF`, `5SHIFT.DBF` usw.). Er wird nur gelesen. Datensatz und
  Bericht dürfen nicht darin liegen.
- `--month`: der (erste) Planungsmonat. Mehrere Monate auf einmal mit `--to YYYY-MM`.
- `--mapping`: die Zuordnung.
- `--corrections`: die Korrekturdatei. **Beim allerersten Export weglassen**: Eine Datei, die es nicht gibt,
  beendet den Export mit `Fehler: Korrekturdatei korrekturen.json nicht lesbar (FileNotFoundError)`. Der erste
  Export ist also derselbe Befehl ohne `--corrections korrekturen.json`:

  ```sh
  python3 ../sp5-export/sp5_export.py --data <SP5-Ordner> --month 2027-01 --mapping mapping.json \
    --report bericht-2027-01.csv --hidden-shifts-months 3 --recent-months 3 \
    --out dataset-2027-01.json 2> export-2027-01.log
  ```
- `--report`: der Bericht als CSV, siehe unten.
- `--hidden-shifts-months 3`: in SP5 ausgeblendete Schichten werden übernommen, wenn sie in den Planungsmonaten oder
  den 3 Monaten davor Dienste haben. 3 ist der Standard; 0 übernimmt keine.
- `--recent-months 3`: Bereiche, in denen eine Person in ihren letzten 3 Monaten mit Diensten nicht mehr gearbeitet
  hat (davor aber schon), werden für sie "nur auf Anfrage". 3 ist der Standard; 0 schaltet das ab.
- `--out`: der Datensatz für Dispofold.
- `2> export-2027-01.log`: schreibt die Meldungen (stderr) in eine Protokolldatei statt auf den Bildschirm. Ohne
  diesen Teil erscheinen sie in der Konsole.

Für den ersten Lauf mit einem neuen Bestand empfiehlt das README zusätzlich `--areas-from-history 3`; danach
`--shifts-from-history 3`, wenn die Zeilen `schicht` des Berichts das nahelegen. Die Standardwerte der übrigen
Optionen (12 Monate Historie, Bedarf aus der Historie, Schichtmix ab 10 Diensten) passen meist.

### Das Protokoll lesen

Die letzte Zeile fasst den Lauf zusammen:

```
1 Planungsmonate, 150 Mitarbeiter, 13 Schichten, 17 Bedarfsregeln, 34 Warnungen; geschrieben: dataset-2027-01.json
```

```sh
tail -n 1 export-2027-01.log
grep -c '^Warnung:' export-2027-01.log
```

Davor steht je Warnung eine Zeile `Warnung: ...`. Warnungen nennen nur SP5-Ids, Kürzel und Anzahlen, nie Namen
(zum Weitergeben siehe [6.](#6-daten-für-eine-fehlersuche-weitergeben)). Viele Zeilen kommen bei jedem Lauf und sind
Zählerstände, keine Fehler (die Tabelle Warnungen im README sagt zu jeder, was sie bedeutet). Zuerst ansehen:

- `... Mitarbeiter nicht übernommen (Grund)`: mit dem erwarteten Personalstand vergleichen.
- `Bereich AN: ... ohne den Bereich aus Gruppen, ... mit dem Bereich aus Gruppen ohne Dienst darin`: passen Gruppen
  und Zuordnung zu dem, was die Leute tatsächlich arbeiten?
- `Schicht ohne Bereich ignoriert: ...`, `Gruppe ohne Zuordnung: ...`: in der Zuordnung ergänzen.
- `Bedarf im Bereich ...`: die Spitzen aus der Historie je Tagestyp; ein Bereich mit lauter `0-0` bekommt keinen
  Bedarf (siehe [Fehlerbehebung](#7-fehlerbehebung)).
- mit `--corrections`: `Korrekturen: ... Mitarbeiter korrigiert, ... Felder angewendet, ... Felder übersprungen`
  und je übersprungenem Feld eine Zeile `Korrektur E1002 weeklyHours übersprungen: <Grund>`.

```sh
grep 'Mitarbeiter nicht übernommen (' export-2027-01.log
grep '^Warnung: Korrektur' export-2027-01.log
```

**Fehler.** Bei einem Fehler steht `Fehler: ...` im Protokoll, das Programm endet mit Exit-Code 1 und schreibt
weder Datensatz noch Bericht; vorhandene Dateien gleichen Namens bleiben unverändert. Den Exit-Code zeigt
`echo $?` (Linux) oder `echo %ERRORLEVEL%` (Eingabeaufforderung) direkt nach dem Aufruf. Typisch sind eine
fehlerhafte Zuordnung (die Meldung nennt jeden Eintrag), eine nicht lesbare oder beschädigte Tabelle oder eine
Korrekturdatei mit falschem `format` oder zu hoher `version`. Ein unerwarteter Fehler nennt nur Fehlerart, Datei
und Zeile (`Fehler: unerwarteter Fehler ... in sp5.py:123`), nie Feldinhalte; diese Zeile genügt für eine
Fehlermeldung.

### Den Bericht lesen

Der Bericht (`--report`) ist eine CSV mit `;` als Trennzeichen und 29 Spalten, eine Zeile je Prüfung. Er enthält
keine Namen, nur SP5-Ids, Personen-Ids (`E<SP5-Id>`), Bereichs- und Schichtkürzel und Zahlen. Zahlen haben einen
Punkt als Dezimaltrennzeichen (`38.5`); in einer deutschen Tabellenkalkulation beim Öffnen den Punkt als
Dezimaltrennzeichen oder die Spalten als Text wählen, sonst können Werte falsch gelesen werden (etwa als Datum). Die erste Spalte
`pruefung` sagt, was die Zeile ist; man filtert am besten danach:

```sh
cut -d';' -f1 bericht-2027-01.csv | sort | uniq -c
grep '^korrektur;' bericht-2027-01.csv
```

| `pruefung` | eine Zeile je | gefüllte Spalten |
|---|---|---|
| `bedarf` | Bereich und Tagestyp (`MON` bis `SUN`, `HOL`) | `bereich`, `tagestyp`, `dienste_historie` (gezählte Dienste), `spitze_historie` (min), `spitze_historie_max`, `spitze_sp5`, `dienste_nicht_uebernommen` (nicht gezählt, ausgeschlossene Gruppe), `dienste_nicht_uebernommen_gezaehlt` (gezählt, Person nicht im Datensatz) |
| `bereich` | Person und Bereich aus Gruppen oder mit Diensten | `person`, `bereich`, `dienste_historie`, `bereich_aus_gruppen` (`ja`/`nein`) |
| `schicht` | Person und Schicht ihrer Bereiche | `person`, `bereich`, `schicht`, `dienste_historie`, `einschraenkung` (`auf_anfrage`, `sp5`, `zu_kurze_historie` oder leer) |
| `person` | übernommene Person | `person`, `dienste_historie`, `wochenstunden`, `bereiche`, `mix_kategorien`, `mix_bereiche`, `auf_anfrage` (Zahl der Einschränkungen "nur auf Anfrage") |
| `nicht_zugeordnet`, `ignoriert` | Schicht ohne Bereich mit Diensten | `schicht`, `dienste_historie`, `stunden_historie` |
| `verworfen` | Grund und Schicht verworfener Dienste der Historie | `schicht`, `dienste_historie`, `stunden_historie`, `grund`, `sp5_schicht` |
| `wochenstunden` | Person, bei der `HRSWEEK` vom Wert nach `CALCBASE` abweicht | `person`, `calcbase`, `hrsweek`, `wochenstunden_calcbase`, `wochenstunden` (Wert im Datensatz) |
| `korrektur` | Feld der Korrekturdatei | siehe unten |
| `ausgeblendet` | ausgeblendete Schicht mit Diensten | siehe unten |
| `nicht_mehr` | Person und Bereich, den sie nicht mehr arbeitet | siehe unten |

Die Spalte `person` ist die Id im Datensatz (`E` und die SP5-Id, wie in Dispofold), `sp5_schicht` die SP5-Id
einer Schicht. Die übrigen
Spalten einer Zeile bleiben leer. Was jede Prüfung bedeutet und was zu tun ist, steht im README im Abschnitt der
jeweiligen Option. Drei Prüfungen braucht man beim Bereinigen am häufigsten:

**`korrektur`** (nur mit `--corrections`): je Feld der Korrekturdatei eine Zeile mit `person`, `feld`, `status`,
`wert_export` (der Wert aus SP5 und der Historie) und `wert_korrektur` (der Wert aus der Datei), Regeln und
Einschränkungen als Anzahl.

- `status` `angewendet`: Die Korrektur steht im Datensatz.
- `status` `gleich_quelle`: Die Korrektur steht im Datensatz, ihr Wert ist aber derselbe wie der aus SP5 und der
  Historie (Listen in beliebiger Reihenfolge). Sie schadet nicht und bleibt in jeder Korrekturdatei aus Dispofold;
  sie wirkt also weiter, auch wenn sich der Wert in SP5 später ändert. Wer den Wert wieder SP5 überlassen will,
  löscht das Feld in `korrekturen.json` von Hand.
- `status` `ausgeschlossen` (Feld `exclude`): Die Person fehlt im Datensatz, mit allem, was sich auf sie bezieht.
- `status` `person_fehlt`: Die Person wird gar nicht exportiert (etwa schon in SP5 ausgetreten). Die Korrektur
  fehlt in der nächsten Korrekturdatei aus Dispofold von selbst; nur `exclude` einer Person, die in Dispofold
  nicht mehr aus dem Quellsystem übernommen wird, bleibt darin, bis man das dort ausschaltet. Das schadet nicht.
- `status` `ungueltig`, Grund in `grund`: Das Feld wurde übersprungen, der Datensatz hat den Wert aus SP5. In
  Dispofold neu korrigieren oder in der Datei berichtigen: Die nächste Korrekturdatei aus Dispofold enthält nur,
  was angewendet wurde.
- Bedarf: je Bereich eine Zeile ohne Person mit `feld` `demand:<Bereich>`; `wert_export` und `wert_korrektur` sind
  die Zahl der Bedarfsregeln vorher und nachher.

**`ausgeblendet`**: je in SP5 ausgeblendeter Schicht mit Diensten zwischen Beginn der Historie und Ende der
Planungsmonate eine Zeile mit `schicht`, `dienste_historie`, `stunden_historie`, `sp5_schicht`, `status`
(`uebernommen` oder `verworfen`, dann mit Grund in `grund`, etwa `nicht_in_gebrauch`) und `letzter_dienst`.
Übernommen wird eine ausgeblendete Schicht, wenn sie in den Planungsmonaten oder den `--hidden-shifts-months`
Monaten davor Dienste hat.

**`nicht_mehr`**: je Person und Bereich, den sie nicht mehr arbeitet (`--recent-months`), eine Zeile mit
`person`, `bereich`, `dienste_historie` (Dienste im Bereich in der Historie), `einschraenkung` `auf_anfrage` und
`letzter_dienst`. Die Person behält den Bereich, wird dort aber nur noch auf Anfrage eingeplant.

Beispielzeilen (erfunden, gekürzt auf die gefüllten Spalten):

```
korrektur;E1002: feld weeklyHours, status angewendet, wert_export 39.0, wert_korrektur 38.5
korrektur;E1003: feld exclude, status ausgeschlossen, wert_korrektur true
korrektur;(ohne Person): feld demand:TS, status angewendet, wert_export 0 Regeln, wert_korrektur 1 Regeln
korrektur;E1004: feld weeklyHours, status ungueltig, grund muss eine Zahl größer 0 sein, wert_export 30.0
ausgeblendet;schicht A2, dienste_historie 67, stunden_historie 804.0, sp5_schicht 2, status uebernommen, letzter_dienst 2026-12-31
nicht_mehr;E1012: bereich DR, dienste_historie 23, einschraenkung auf_anfrage, letzter_dienst 2026-08-14
```

### Ausprobieren ohne echte Daten

Ein synthetischer SP5-Ordner mit erfundenen Personen entsteht mit dem Werkzeug der Tests (aus dem
Repository-Ordner, braucht die Pakete aus `requirements-dev.txt` nicht):

```sh
cd sp5-export
python3 -m tools.sp5writer --month 2027-01 --out ../arbeit/sp5
cp mapping.example.json ../arbeit/mapping.json
```

Danach funktionieren alle Befehle dieser Anleitung mit `--data sp5`. Der synthetische Ordner hat Dienste nur in
der letzten Woche vor dem Monat; Bedarf aus der Historie und Schichtmix fallen deshalb dünn aus, und der Bereich
`TS` bekommt keinen Bedarf.

## 3. Bereinigen: Korrekturen in Dispofold

Was in SP5 falsch oder veraltet ist, korrigiert man in Dispofold und gibt es als Korrekturdatei an den Export
zurück. So bleibt es bei jedem neuen Export erhalten, ohne dass SP5 bereinigt werden muss.

1. Den Datensatz importieren (siehe [4. Importieren](#4-importieren)).
2. In Dispofold korrigieren:
   - **Personen**, Seite der Person, **Bearbeiten**: Wochenstunden, Bereiche, Stundenkonto, Ersatzruhe offen,
     Eintritt und Austritt, gewohnte Verteilung, Familienregeln und Einschränkungen. Unter **Einschränkungen** setzt
     **Ganzer Bereich** (**keine**, **nur auf Anfrage** oder **nie**) alle Schichten eines Bereichs auf einmal, für
     jeden Tag; so lässt sich etwa ein Bereich aus `--recent-months` wieder freigeben.
   - Für Personen, die nicht geplant werden sollen (etwa Ausgetretene ohne Austrittsdatum in SP5): auf der Seite
     der Person **Aus dem Quellsystem nicht mehr übernehmen** einschalten (Dispofold ab 0.16.0). Die Person bleibt
     in Dispofold mit ihrer Historie und den abgeschlossenen Monaten und wird danach nicht mehr geplant. In der
     Korrekturdatei wird daraus `"exclude": true`; der Export lässt sie weg, Dispofold behält sie beim Import
     weiter ausgeschlossen. Eine Person ohne Historie und ohne abgeschlossene Monate lässt sich auch mit **Person
     entfernen** ausschließen.
   - **Einstellungen**, **Stammdaten**, **Bedarf**: Bedarf eines Bereichs anlegen oder ändern. In der Datei ersetzt
     das alle Bedarfsregeln des Bereichs.
3. **Einstellungen**, **Daten**, Abschnitt **Korrekturdatei**: **Änderungen als Korrekturdatei exportieren**.
   Dispofold speichert alle geltenden Korrekturen als `korrekturen.json` (Format `dispofold/corrections`, Version
   2): die, die dieser Export beim letzten Import schon angewendet hatte, und die seit dem Import geänderten
   Stammdaten, je Person und Feld mit dem neueren Wert. Die Meldung nennt, wie viele Einträge aus früheren
   Korrekturen stammen und wie viele seit dem Import geändert wurden. Gibt es keine Korrektur, meldet Dispofold
   das und speichert keine Datei; ab Dispofold 0.16.0 speichert es eine Datei ohne Einträge, wenn seit dem Import
   etwas geändert und wieder zurückgenommen wurde (etwa ein Ausschluss), damit sie die bisherige ersetzt. Was die Datei nicht ausdrücken kann, steht darunter unter "Nicht in der
   Korrekturdatei, bitte im Quellsystem nachtragen:".
4. Die bisherige `korrekturen.json` im Arbeitsordner durch die neue Datei ersetzen und beim nächsten Export
   `--corrections korrekturen.json` angeben (der Befehl aus [2. Exportieren](#2-exportieren)). Im Protokoll und in
   den Zeilen `korrektur` des Berichts prüfen, dass alles `angewendet`, `gleich_quelle` oder `ausgeschlossen` ist.

In Dispofold angelegte Personen und geänderte Namen (Vorname, Nachname, Kürzel, auch die externe Id) kann die
Korrekturdatei nicht ausdrücken; die Vorschau beim Import listet sie weiter unter "Nicht übernommen:". Solche
Personen in SP5 eintragen oder nach dem Import in Dispofold neu anlegen, Namen in SP5 ändern oder nach dem Import
in Dispofold nachtragen. Ebenso nicht in der Datei: Änderungen an Bereichen, Schichten, Abwesenheitsarten und
Feiertagen (dafür die Zuordnung oder SP5 ändern).

**Jede weitere Runde** geht genauso: in Dispofold korrigieren, als Korrekturdatei exportieren, `korrekturen.json`
durch die neue Datei ersetzen, mit `--corrections korrekturen.json` exportieren, importieren. Von Hand
zusammenführen muss man nichts. Das geht, weil der Datensatz im Abschnitt `appliedCorrections` festhält, welche
Korrekturen der Export angewendet hat (ohne `--corrections` leer), und Dispofold diese mit den Änderungen seit dem
Import zusammen exportiert. Ein Feld, das in Dispofold auf den Wert aus SP5 zurückgeändert wird, bleibt mit
diesem Wert in der Datei (im Bericht dann `gleich_quelle`).

Voraussetzung ist Dispofold ab Version 0.15.0. Ältere Versionen importieren den Datensatz zwar, übergehen den
Abschnitt aber und exportieren nur die Änderungen seit dem Import, ohne Hinweis; dann Dispofold aktualisieren.
Meldet Dispofold beim Export der Korrekturdatei, dass der letzte Import die schon angewendeten Korrekturen nicht
nennt (ein Datensatz aus einer älteren Version dieses Exports), die bisherige `korrekturen.json` nicht ersetzen,
sondern erst mit ihr neu exportieren, mit **Änderungen aus Dispofold behalten** importieren (sonst gehen die
Änderungen seit dem letzten Import verloren) und dann die Korrekturdatei speichern. Das ist einmal nötig, nach
dem Wechsel auf diese Version des Exports und Dispofold 0.15.0.

Was beim Ersetzen nicht in die neue Datei kommt:

- `note`: Notizen kommen nie in den Datensatz und deshalb auch nicht in die Datei aus Dispofold. Wer sie braucht,
  hält sie getrennt fest.
- Felder mit `status` `ungueltig` oder `person_fehlt`: Sie wurden nicht angewendet (siehe
  [Den Bericht lesen](#den-bericht-lesen)).
- Eine Person mit `"exclude": true` bleibt in jeder neuen Datei ausgeschlossen, bis man in Dispofold **Aus dem
  Quellsystem nicht mehr übernehmen** wieder ausschaltet (ab Dispofold 0.16.0; der Schalter steht auch bei einem
  Ausschluss aus einer früheren Datei, solange Dispofold die Person kennt). Die nächste Korrekturdatei enthält sie
  dann nicht mehr, auch wenn sonst nichts darin steht; mit ihr neu exportieren, dann ist die Person wieder im
  Datensatz. Kennt Dispofold die Person nicht (etwa nach einer Neuinstallation), oder mit einer älteren Version,
  löscht man ihren Eintrag in `korrekturen.json` von Hand und exportiert neu.

Eine Korrekturdatei lässt sich auch von Hand schreiben; ihr Format und alle Prüfungen stehen im README unter
Korrekturdatei. Ein erfundenes Beispiel der Version 2:

```json
{"format": "dispofold/corrections", "version": 2,
 "people": {"E1002": {"weeklyHours": 38.5, "note": "Vertrag geändert, in SP5 noch alt"},
            "E1003": {"exclude": true}},
 "demand": {"TS": [{"days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"], "date": null,
                    "from": "08:00", "to": "16:30", "min": 1, "max": 1}]}}
```

`note` ist nur für die Planung und kommt nie in den Datensatz oder den Bericht.

## 4. Importieren

In Dispofold unter **Einstellungen**, **Daten**, Abschnitt **Import**: bei **Datensatz importieren** die Datei
`dataset-2027-01.json` wählen.

- **Ohne Änderungen in Dispofold** seit dem letzten Import: **Datensatz importieren**. Dispofold prüft die ganze
  Datei; ist etwas falsch, wird nichts übernommen und die Fehler erscheinen mit Ids. Danach nennt die Seite, was
  importiert wurde (Personen, Bereiche, Schichten, Monate), und bietet an, zum Monat zu wechseln.
- **Mit Änderungen in Dispofold**, die noch nicht über eine Korrekturdatei im Datensatz stecken: das Häkchen
  **Änderungen aus Dispofold behalten** setzen (es ist schon gesetzt, wenn seit dem letzten Import etwas geändert
  wurde) und **Datensatz importieren**. Ohne Häkchen fragt Dispofold nach ("Stammdaten wurden geändert."):
  **Datensatz exportieren** sichert den jetzigen Stand, **Änderungen behalten** zeigt die Vorschau, **Trotzdem
  importieren** ersetzt die Stammdaten und die Änderungen gehen verloren, **Abbrechen** importiert nichts.

Die **Vorschau** ("Vorschau des Imports mit den Änderungen aus Dispofold") zeigt:

- "Übernommen aus Dispofold: ..." mit der Zahl der Änderungen, die auf den neuen Datensatz angewendet werden (oder
  "Seit dem letzten Import wurde in Dispofold nichts geändert.");
- "Auch im neuen Import geändert, es gilt der Wert aus Dispofold:" mit Person und Feld oder "Bedarf im Bereich
  ..."; das sind Felder, die auch der neue Datensatz gegenüber dem letzten Import geändert hat. Eine Änderung,
  die schon mit demselben Wert über `--corrections` im neuen Datensatz steckt, steht nicht hier;
- "Nicht übernommen:" mit allem, was sich nicht übertragen lässt, etwa in Dispofold angelegte Personen, geänderte
  Namen oder Änderungen für Personen, die im neuen Datensatz fehlen.

**Jetzt importieren** führt den Import aus. Ein abgeschlossener Monat kann nicht überschrieben werden; vorhandene
Pläne bleiben. Der Import ersetzt die Stammdaten und die Planungsmonate der Datei.

Beide Wege lassen sich kombinieren: Wer mit **Änderungen aus Dispofold behalten** importiert, sollte die
Änderungen trotzdem als Korrekturdatei exportieren, damit sie in jedem späteren Datensatz schon enthalten sind.

## 5. Planen, prüfen, nachbessern, abschließen

1. **Eingaben:** Abwesenheiten, Wünsche und Vorgaben des Monats eintragen und **Speichern**. Abwesenheiten aus SP5
   und Dienste auf `ignoreShifts`-Schichten (als Abwesenheit `EXT`) kommen schon mit dem Datensatz.
2. **Dienstplan**, **Planen**. Der Solver rechnet einige Minuten im Hintergrund; **Stopp** beendet früher.
3. **Prüfen**, **Probleme**: die Zeile über dem Plan sagt, ob er verwendbar ist ("Plan verwendbar: keine
   Regelverstöße, keine Lücken" in Grün). Unter **Prüfen** stehen alle Regelverstöße, der **Offene Bedarf** mit
   **Auf Anfrage verfügbar** und im Abschnitt **Stammdaten** Auffälligkeiten wie "Bereich TS hat keinen Bedarf."
   oder Personen ohne Bereich. Fehler in den Stammdaten wie in [3. Bereinigen](#3-bereinigen-korrekturen-in-dispofold)
   korrigieren.
4. **Nachbessern:** Zellen im Dienstplan von Hand ändern (sie sind dann fixiert), Lücken über **Auf Anfrage
   verfügbar** mit einem Klick auf einen Namen füllen, bei Bedarf noch einmal **Planen**.
5. **Monat abschließen** im Menü **Ausgabe** des Dienstplans oder unter **Einstellungen**, **Daten**. Der Dialog
   prüft Regelverstöße, offene Lücken und "Plan veraltet"; ist etwas offen, geht es nur mit **Trotzdem
   abschließen**. Die Zahlen des Monats kommen in die Historie. Monate werden der Reihe nach abgeschlossen.

Vor dem Abschluss ist ein guter Zeitpunkt für **Änderungen als Korrekturdatei exportieren**, damit der Export für
den Folgemonat die Korrekturen schon mitbringt.

## 6. Daten für eine Fehlersuche weitergeben

Datensatz (`dataset-*.json`) und Exporte aus Dispofold enthalten Namen und personenbezogene Daten (Wochenstunden,
Stundenkonten, Einschränkungen, Familienregeln) und gehören nicht in E-Mails, Tickets oder Repositories.

**Ohne Namen, aber nicht anonym** sind:

- das Protokoll des Exports (`export-*.log`): SP5-Ids, Kürzel und Zahlen, dazu in der letzten Zeile und in
  Fehlermeldungen die Dateipfade so, wie sie beim Aufruf angegeben wurden;
- der Bericht (`bericht-*.csv`): Personen-Ids aus SP5 mit Wochenstunden, Bereichen und Diensten je Person, dazu
  die Kürzel des Betriebs;
- die Zuordnung: Gruppennamen und Kürzel des Betriebs, keine Personen.

Diese Dateien nur an jemanden geben, der die Ids und Kürzel des Betriebs sehen darf. Für alle anderen sind die
pseudonymisierten Dateien aus Dispofold der Weg.

Die Korrekturdatei enthält keine Namen, aber Freitext in `note` und in den Notizen der Familienregeln
(`familyRules`); vor dem Weitergeben die Notizen durchsehen.

**Datensatz und Plan:** In Dispofold unter **Einstellungen**, **Daten**, Abschnitt **Anonymisieren**:
**Pseudonymisieren** erstellt Datensatz und Plan des gewählten Monats mit Pseudonymen (P001, P002, ...) statt
Namen; Namen, Kürzel und Ids der Personen werden ersetzt, Namen von Bereichen, Schichten und Abwesenheitsarten
durch ihr Kürzel, Notizen der Familienregeln geleert. **Datensatz speichern** und **Plan speichern** speichern die
beiden Dateien zum Weitergeben. **Schlüssel speichern** speichert die Zuordnung der Pseudonyme zu den Personen:
Der Schlüssel bleibt in der Planung und wird nie weitergegeben. Steht nach dem Pseudonymisieren noch ein Name in
den Dateien, exportiert Dispofold nichts und nennt die Stelle; dort umbenennen und noch einmal pseudonymisieren.

Pseudonymisierte Dateien enthalten weiter Wochenstunden, Einschränkungen, Abwesenheiten und Dienste je Pseudonym.
Sie sind nicht anonym im strengen Sinn, nur ohne Namen: an möglichst wenige und nur für die Fehlersuche weitergeben.

## 7. Fehlerbehebung

**Personen mit "!" im Namen.** Der Export liest Namen und Kürzel aus SP5 unverändert und wertet sie nicht aus; ein
"!" im Namen kommt so in Dispofold an. Ob jemand übernommen wird, hängt nur von `HIDE`, der Beschäftigung
(`EMPSTART`/`EMPEND`), den Gruppen (`excludeGroups`), den Wochenstunden und den Bereichen ab. Sind solche Einträge
keine Personen, die geplant werden sollen, sie in SP5 ausblenden, ihr Austrittsdatum eintragen oder sie in eine
Gruppe aus `excludeGroups` nehmen, oder in Dispofold mit **Aus dem Quellsystem nicht mehr übernehmen** (oder
**Person entfernen**) ausschließen und als Korrekturdatei exportieren (`exclude`). Ihre Dienste zählen weiter für den Bedarf aus der Historie, außer mit `excludeGroups`.

**Bereich ohne Bedarf.** Dispofold meldet unter **Prüfen**, **Stammdaten** "Bereich TS hat keinen Bedarf." mit dem
Hinweis "Bedarf für den Bereich anlegen, sonst zählt jeder Dienst dort als Überbesetzung." Im Protokoll steht für
den Bereich `Bedarf im Bereich TS, Spitze je Tagestyp aus der Historie/aus SP5: MON 0-0/1, ...`: Der Bedarf kommt aus
der Historie, und dort war im Bereich nie jemand gleichzeitig im Dienst (zu wenige Dienste, nur Personen aus
`excludeGroups`, oder die Schichten fehlen in `shifts`). Abhilfe: unter **Einstellungen**, **Stammdaten**, **Bedarf**
den Bedarf anlegen und als Korrekturdatei exportieren (`demand.TS`), die Zuordnung prüfen oder zum Vergleich
`--demand sp5` verwenden.

**Ausgeblendete Schichten in Gebrauch.** In SP5 ausgeblendete Schichten, die noch gearbeitet werden, übernimmt
der Export, wenn sie in den Planungsmonaten oder den `--hidden-shifts-months` Monaten davor Dienste haben. Das
Protokoll nennt die Zahlen (`Ausgeblendete Schichten: ... übernommen, weil in Gebrauch (...), ... nicht
übernommen`), der Bericht je Schicht eine Zeile `ausgeblendet`. Ist eine übernommene Schicht nicht mehr in
Verwendung, sie in `ignoreShifts` aufnehmen oder in SP5 aufräumen; wird sie gebraucht, in SP5 wieder einblenden.
Eine verworfene Schicht mit spätem `letzter_dienst` spricht für ein größeres `--hidden-shifts-months`.

**Wochenstunden mit Nachkommastellen.** Der Export rechnet die Wochenstunden wie SP5 nach `CALCBASE` und rundet
auf zwei Stellen, etwa `38.46` aus Stunden je Tag mal Arbeitstagen. Dispofold-Versionen vor 0.8.0 konnten eine
Person mit solchen Werten im Formular nicht speichern, auch wenn nur ein anderes Feld geändert wurde; dann
Dispofold aktualisieren. Ist der Wert selbst falsch, in SP5 die Felder der Grundlage korrigieren (`HRSDAY` und
`WORKDAYS`, `HRSMONTH` oder `HRSTOTAL`) oder in Dispofold die Wochenstunden ändern und als Korrekturdatei exportieren
(`weeklyHours`, in JSON mit Punkt: `38.5`). Welche Personen betroffen sind, zeigen die Zeilen `wochenstunden` des
Berichts.

**Ausgetretene.** Wer in SP5 kein Austrittsdatum hat und nicht ausgeblendet ist, gilt als beschäftigt und wird
übernommen. Abhilfe: das Austrittsdatum (`EMPEND`) in SP5 eintragen, oder in Dispofold auf der Seite der Person
**Aus dem Quellsystem nicht mehr übernehmen** einschalten (in der Korrekturdatei `exclude`, auch für Personen, die
in abgeschlossenen Monaten oder in der Historie vorkommen) und als Korrekturdatei exportieren. Alternativ in
Dispofold den **Austritt** eintragen (`exitDate`). Ihre Dienste zählen weiter für den Bedarf aus der Historie: Die Arbeit, die sie gemacht hat, müssen andere übernehmen. Eine Korrektur
für jemanden, den SP5 inzwischen selbst nicht mehr exportiert, steht im Bericht mit `status` `person_fehlt` und
fehlt in der nächsten Korrekturdatei aus Dispofold von selbst (außer `exclude`, siehe oben).
