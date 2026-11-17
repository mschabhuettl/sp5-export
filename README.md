# sp5-export

Kommandozeilenwerkzeug, das einen oder mehrere Planungsmonate aus einem Schichtplaner5-Datenordner (SP5) liest und als
**eine** JSON-Datei im Datensatzformat von Dispofold (`dispofold/dataset`, Version 2)
schreibt. Der SP5-Ordner wird nur gelesen (alle Tabellen werden im Modus `rb` geöffnet), nie verändert.
Kein Dienst, kein Server, kein Docker.

Dispofold ist ein Dienstplanungswerkzeug für Leitstellen. Die echte Zuordnungsdatei (Mapping) mit den
Kürzeln und Gruppennamen des jeweiligen Betriebs ist eine lokale Datei des Eigentümers und gehört nie in
ein Repository; im Repository liegt nur das neutrale Beispiel `mapping.example.json`.

## Voraussetzungen

- Python 3.10 oder neuer. Weitere Pakete braucht der Export nicht; die SP5-Lesefunktionen liegen in
  `vendor/sp5lib` bei.
- Lesezugriff auf den SP5-Datenordner (der Ordner mit `5EMPL.DBF`, `5SHIFT.DBF` usw.). Das darf eine
  Netzwerkfreigabe sein; ein schreibgeschützter Zugang genügt.

## Aufruf

```
python3 sp5_export.py --data <SP5-Ordner> --month YYYY-MM --mapping mapping.json --out dataset.json
```

Linux (Freigabe z. B. unter `/mnt/sp5` eingebunden):

```sh
python3 sp5_export.py --data /mnt/sp5/Daten --month 2027-01 --mapping mapping.json --out dataset-2027-01.json
```

Windows (Eingabeaufforderung oder PowerShell; Laufwerksbuchstabe oder UNC-Pfad):

```bat
py -3 sp5_export.py --data "S:\SP5\Daten" --month 2027-01 --mapping mapping.json --out dataset-2027-01.json
py -3 sp5_export.py --data "\\server\sp5\Daten" --month 2027-01 --mapping mapping.json --out dataset-2027-01.json
```

### Optionen

| Option | Bedeutung |
|---|---|
| `--to YYYY-MM` | letzter Planungsmonat (höchstens 12 Monate nach `--month`). Der Datensatz hat dann je Monat einen Planungsmonat mit dessen Abwesenheiten; Bedarf und Feiertage gelten für alle Monate. Übernommen wird, wer in mindestens einem der Monate beschäftigt ist. |
| `--history-months N` | Monate Historie vor `--month`, 0 bis 24, Standard 12. Mit 0 ist `--demand sp5` nötig. |
| `--demand history\|sp5` | Bedarf aus der tatsächlichen Besetzung in der Historie (Standard) oder aus dem SP5-Bedarf (siehe unten) |
| `--areas-from-history N` | ergänzt Bereiche, in denen jemand in der Historie mindestens N Dienste hatte (siehe unten) |
| `--shifts-from-history N` | Schichten der eigenen Bereiche mit weniger als N Diensten in der Historie werden "nur auf Anfrage" (siehe unten) |
| `--recent-months N` | Bereiche einer Person ohne Dienst in ihren letzten N Monaten mit Diensten (aber mit Diensten davor in der Historie) werden "nur auf Anfrage", Standard 3; 0 schaltet das ab (siehe [Bereiche, die nicht mehr gearbeitet werden](#bereiche-die-nicht-mehr-gearbeitet-werden)) |
| `--mix-min-duties N` | Schichtmix nur für Personen mit mindestens N Diensten in der Historie, Standard 10 (siehe unten) |
| `--hidden-shifts-months N` | ausgeblendete Schichten (`HIDE`) übernehmen, wenn sie in den Planungsmonaten oder den N Monaten davor Dienste haben, Standard 3; 0 übernimmt keine (siehe [Ausgeblendete Schichten in Gebrauch](#ausgeblendete-schichten-in-gebrauch)) |
| `--no-shift-mix` | keinen Schichtmix schreiben; der Datensatz ist dann derselbe wie vor dieser Option |
| `--report DATEI` | CSV mit SP5-Ids und Bedarfsvergleich, eine Zeile je Person zum Durchsehen, verworfene Dienste der Historie je Grund und Schicht, abweichende Wochenstunden, Korrekturen (siehe unten) |
| `--corrections DATEI` | Korrekturdatei mit von Hand korrigierten Stammdaten einzelner Personen, ab Version 2 auch ausgeschlossenen Personen und Bedarf je Bereich, zuletzt angewendet (siehe [Korrekturdatei](#korrekturdatei)) |

Vormonatsdienste stehen nur im ersten Planungsmonat. Für die späteren nimmt Dispofold die Dienste aus dem
gespeicherten Plan des Monats davor; ist der noch nicht geplant, beginnt der Monat ohne Vormonatsdienste.

Mit 12 Monaten Historie wird in Dispofold die Durchrechnung (52 Wochen, `averageWeekHours`) zur harten Regel:
Dispofold verlangt dafür je Person einen Historieneintrag in jedem der 11 Monate vor dem Planungsmonat. Mit
weniger Historie zeigt Dispofold den Durchschnitt nur an.

### Bereiche aus der Historie

Die Bereiche einer Person kommen aus ihren SP5-Gruppen (`groups`). Fehlt dort eine Zuordnung, plant Dispofold
die Person für Bereiche, in denen sie tatsächlich arbeitet, nicht ein. Deshalb vergleicht der Export bei
jedem Lauf die Dienste der Historie (nur Schichten mit Bereich, Sonderdienste wie oben) mit den Bereichen aus
Gruppen und meldet auf stderr nur Zahlen, nie Namen:

- je Bereich: wie viele mit Diensten darin, aber ohne den Bereich aus Gruppen; wie viele mit dem Bereich aus
  Gruppen, aber ohne Dienst darin;
- wie viele ohne Bereich aus Gruppen zugeordnete Schichten gearbeitet haben.

`--areas-from-history N` gibt jeder Person zusätzlich die Bereiche, in denen sie in der Historie mindestens
N Dienste hatte; Personen ohne Bereich aus Gruppen werden damit übernommen, wenn sie N erreichen.
`--report bericht-2027-01.csv` schreibt dazu eine CSV (Trennzeichen `;`), damit sich die Personen in SP5
nachschlagen lassen:

```
pruefung;person;bereich;schicht;dienste_historie;bereich_aus_gruppen;einschraenkung;tagestyp;spitze_historie;spitze_sp5;dienste_nicht_uebernommen;stunden_historie;spitze_historie_max;wochenstunden;bereiche;mix_kategorien;mix_bereiche;auf_anfrage;grund;sp5_schicht;calcbase;hrsweek;wochenstunden_calcbase;feld;status;wert_export;wert_korrektur;dienste_nicht_uebernommen_gezaehlt;letzter_dienst
bedarf;;AN;;412;;;MON;3;8;17;;4;;;;;;;;;;;;;;;23;
bereich;E12;AN;;41;ja;;;;;;;;;;;;;;;;;;;;;;;
person;E12;;;46;;;;;;;;;40.0;AN DR;DAY 0.891, NIGHT 0.109;AN 0.696, DR 0.304;2;;;;;;;;;;;
schicht;E12;AN;AN-N;0;;auf_anfrage;;;;;;;;;;;;;;;;;;;;;;
nicht_zugeordnet;;;S1;120;;;;;;;960.0;;;;;;;;;;;;;;;;;
verworfen;;;S1;120;;;;;;;960.0;;;;;;;nicht_zugeordnet;31;;;;;;;;;
wochenstunden;E12;;;;;;;;;;;;40.0;;;;;;;2;38.5;40.0;;;;;;
korrektur;E12;;;;;;;;;;;;;;;;;;;;;;weeklyHours;angewendet;40.0;30.0;;
ausgeblendet;;;A2;67;;;;;;;804.0;;;;;;;;2;;;;;uebernommen;;;;2026-12-31
nicht_mehr;E12;DR;;23;;auf_anfrage;;;;;;;;;;;;;;;;;;;;;;2026-08-14
```

Der Bericht hat 29 Spalten. Wer ihn nach Spaltennummer einliest: die Spalten `feld` bis `wert_korrektur`,
`dienste_nicht_uebernommen_gezaehlt` und `letzter_dienst` sind neu und stehen am Ende.

Zeilen `bereich` je Person und Bereich, den sie aus Gruppen hat oder in dem sie gearbeitet hat
(`bereich_aus_gruppen` ja oder nein); Zeilen `schicht` je Person und Schicht ihrer Bereiche (siehe
[Schichten aus der Historie](#schichten-aus-der-historie)); Zeilen `bedarf` je Bereich und Tagestyp ohne Person
(siehe [Bedarf aus der Historie](#bedarf-aus-der-historie)); Zeilen `nicht_zugeordnet` und `ignoriert` je
Schicht ohne Bereich, ohne Person, mit `dienste_historie` und `stunden_historie` (siehe
[Dienste auf Schichten ohne Bereich](#dienste-auf-schichten-ohne-bereich)); Zeilen `person` je übernommener
Person mit den angehängten Spalten `wochenstunden` bis `auf_anfrage` (siehe
[Schichtmix aus der Historie](#schichtmix-aus-der-historie)); Zeilen `verworfen` je Grund und Schicht mit
`grund` und `sp5_schicht` (siehe [Verworfene Dienste der Historie](#verworfene-dienste-der-historie)); Zeilen
`wochenstunden` je Person, bei der `HRSWEEK` vom Wert nach `CALCBASE` abweicht (siehe
[Wochenstunden nach CALCBASE](#wochenstunden-nach-calcbase)); Zeilen `korrektur` je Feld der Korrekturdatei
(siehe [Korrekturdatei](#korrekturdatei)); Zeilen `ausgeblendet` je ausgeblendeter Schicht mit Diensten (siehe
[Ausgeblendete Schichten in Gebrauch](#ausgeblendete-schichten-in-gebrauch)); Zeilen `nicht_mehr` je Person und
Bereich, den sie nicht mehr arbeitet (siehe
[Bereiche, die nicht mehr gearbeitet werden](#bereiche-die-nicht-mehr-gearbeitet-werden)); die übrigen Zeilen
lassen diese Spalten leer. Die Datei enthält keine Namen, nur SP5-Ids und
Kürzel, liegt neben dem Datensatz und ist wie dieser per `.gitignore` ausgeschlossen.

Empfehlung für den ersten Lauf: `--areas-from-history 3 --report bericht.csv`, dann den Bericht mit der
Zuordnung vergleichen und die Zuordnung oder die Gruppen in SP5 korrigieren.

Ausbildungsdienste zählen nicht, wenn ihre Schichten wie vorgesehen in `ignoreShifts` stehen: Sie haben dann
keinen Bereich. Bewusst nicht aus der Historie abgeleitet werden Wünsche und Einschränkungen nach Art "nie":
Gewohnheiten der Vergangenheit sind keine Regeln.

### Dienste auf Schichten ohne Bereich

Dienste auf Schichten, die die Zuordnung keinem Bereich zuweist, kommen nicht in den Datensatz; ihre Stunden
hätten in Dispofold keinen Platz. Der Export meldet sie bei jedem Lauf (außer ohne Historie) nur als Zahlen und
Kürzel, je Schicht mit mindestens einem Dienst, nach Stunden absteigend:

```
Warnung: Schicht 'S1' nicht zugeordnet: 120 Dienste, 960.0 Stunden in den 12 Monaten davor
Warnung: Schicht 'X2' ignoriert: 40 Dienste, 320.0 Stunden in den 12 Monaten davor
Warnung: Schichten ohne Bereich: 2 Schichten, 160 Dienste, 1280.0 Stunden in den 12 Monaten davor
```

Gezählt werden die Dienste der Historie wie bei Bereichen und Bedarf, nur von übernommenen Mitarbeitern (nicht
die ausgeschlossener, ausgeblendeter oder nicht beschäftigter) und ohne Tage mit ganztägiger Abwesenheit.
Stunden sind die Dienstzeit der Schicht je Dienst (häufigste Zeit der Wochentage). Ausgeblendete Schichten
fehlen in der Meldung (siehe [Verworfene Dienste der Historie](#verworfene-dienste-der-historie)). `nicht zugeordnet`: In der Zuordnung unter `shifts` eintragen, wenn Dispofold die Schicht
planen soll. `ignoriert`: steht in `ignoreShifts`, also Absicht; nichts zu tun.

### Ausgeblendete Schichten in Gebrauch

In SP5 ausgeblendete Schichten (`HIDE`) werden manchmal weiter gearbeitet. Würde der Export sie weglassen,
fehlten ihre Dienste in Historie, Bedarf und Schichtmix. Deshalb gilt eine ausgeblendete Schicht als in Gebrauch,
wenn sie in den Planungsmonaten oder den N Monaten davor (`--hidden-shifts-months N`, Standard 3, höchstens
so weit zurück wie die Historie) mindestens einen Dienst hat, gleich von wem (Istplan `5MASHI` ohne `TYPE` 1,
Sonderdienste `5SPSHI`, Dienstzyklen). Eine Schicht in Gebrauch behandelt der Export genau wie eine sichtbare:
Zuordnung, `ignoreShifts`, Bedarf, Historie, Mix, Einschränkungen und Dienste der Planungsmonate. Eine
ausgeblendete Schicht ohne Dienste in diesem Zeitraum fällt weg wie bisher; mit `--hidden-shifts-months 0` fallen
alle ausgeblendeten Schichten weg.

Eine Zeile `Ausgeblendete Schichten: 2 übernommen, weil in Gebrauch (...), 3 nicht übernommen` nennt die Zahlen
(nur wenn es ausgeblendete Schichten gibt). Mit `--report` steht je ausgeblendeter Schicht mit Diensten von Beginn
der Historie bis Ende der Planungsmonate eine Zeile `ausgeblendet`: Kürzel (`schicht`), Zahl der Einträge
(`dienste_historie`) und Stunden (`stunden_historie`) in diesem Zeitraum, SP5-Id (`sp5_schicht`), `status`
`uebernommen` oder `verworfen` mit dem Grund in `grund` (`nicht_in_gebrauch`, oder der Grund einer sichtbaren
Schicht wie `ignoriert`, `nicht_zugeordnet`, `zu_lang`), und das Datum des letzten Dienstes (`letzter_dienst`).

Was tun: Ist eine übernommene Schicht wirklich nicht mehr in Verwendung (etwa nur noch alte Einträge in den
letzten Monaten), sie in `ignoreShifts` aufnehmen oder in SP5 aufräumen. Wird sie noch gebraucht, sie in SP5
wieder einblenden. Eine verworfene Schicht mit spätem `letzter_dienst` spricht für ein größeres N.

### Verworfene Dienste der Historie

Die Warnung `26 Dienste 2026-12-01..2026-12-31 mit nicht zugeordneter Schicht ignoriert` zählt mehr als die
Meldung der Schichten ohne Bereich. Der Export teilt deshalb (außer ohne Historie) die Dienste der Historie, die
nicht in den Datensatz kommen, nach Gründen auf, eine Zeile je Grund mit Diensten und Stunden, auch bei 0:

```
Warnung: Verworfene Dienste 2026-12-01..2026-12-31, Schicht nicht zugeordnet: 4 Dienste, 48.0 Stunden
Warnung: Verworfene Dienste 2026-12-01..2026-12-31, Schicht ignoriert (ignoreShifts): 10 Dienste, 80.0 Stunden
Warnung: Verworfene Dienste 2026-12-01..2026-12-31, Schicht ausgeblendet (HIDE): 4 Dienste, 32.0 Stunden
Warnung: Verworfene Dienste 2026-12-01..2026-12-31, Schicht länger als 12 Stunden oder ohne Dienstzeit: 5 Dienste, 39.0 Stunden
Warnung: Verworfene Dienste 2026-12-01..2026-12-31, sonstige: 3 Dienste, 12.0 Stunden (2 ohne Schicht in 5SHIFT, 1 an Tagen mit ganztägiger Abwesenheit, 0 neben einem anderen Eintrag desselben Tags)
Warnung: Verworfene Dienste 2026-12-01..2026-12-31: zusammen 26 Dienste übernommener Mitarbeiter auf Schichten, die nicht übernommen werden (wie die Warnung '... Dienste 2026-12-01..2026-12-31 mit nicht zugeordneter Schicht ignoriert')
Warnung: Verworfene Dienste 2026-12-01..2026-12-31, Mitarbeiter nicht übernommen: 29 Dienste, 256.0 Stunden auf übernommenen Schichten (im Bedarf gezählt außer ausgeschlossene Gruppe), dazu 52 Dienste, 505.0 Stunden auf Schichten, die nicht übernommen werden
```

Wie die Zahlen zusammenpassen:

- Die Warnung `... mit nicht zugeordneter Schicht ignoriert` zählt Einträge übernommener Mitarbeiter (Sonderdienste
  5SPSHI, Ist-Plan 5MASHI, Dienste aus Zyklen) auf einer Schicht, die nicht im Datensatz ist, auch zweite
  Einträge desselben Tags und Einträge an Tagen mit ganztägiger Abwesenheit. Die ersten fünf Gründe teilen
  genau diese Einträge auf; ihre Summe steht in der Zeile `zusammen` und ist die Zahl dieser Warnung (für das
  Fenster der Historie; die Warnung gibt es noch einmal für die Vormonatsdienste der letzten 7 Tage, die hier
  nicht gezählt werden). Ist die Summe 0, fehlt diese Warnung.
- `nicht zugeordnet` und `ignoriert` sind genau die Dienste der Meldung
  [Dienste auf Schichten ohne Bereich](#dienste-auf-schichten-ohne-bereich): je Person und Tag der erste Eintrag,
  wenn der Tag keinen übernommenen Dienst und keine ganztägige Abwesenheit hat. Ausgeblendete und zu lange
  Schichten oder Schichten ohne Dienstzeit werden ebenso gezählt; je Person und Tag zählt aber nur ein Eintrag,
  und hat der Tag einen Dienst einer Schicht ohne Bereich, ist es dieser.
- `Schicht ausgeblendet` und die Zeilen `verworfen` mit Grund `ausgeblendet` betreffen nur ausgeblendete
  Schichten, die nicht in Gebrauch sind; die übrigen werden übernommen (siehe
  [Ausgeblendete Schichten in Gebrauch](#ausgeblendete-schichten-in-gebrauch)).
- `sonstige` sind die übrigen Einträge: Schicht-Id, die es in 5SHIFT nicht gibt (Stunden 0), Einträge an Tagen
  mit ganztägiger Abwesenheit, und Einträge neben einem anderen Eintrag desselben Tags (der Tag hat einen
  übernommenen Dienst, oder ein früherer Eintrag zählt schon).
- `Mitarbeiter nicht übernommen` sind Dienste von Personen außerhalb des Datensatzes. Die Dienste auf übernommenen
  Schichten sind die Summe der `nicht gezählt` und der gezählten `davon ... nicht übernommen` aus den Zeilen
  `Bedarf im Bereich ...`; ihre Einträge auf
  Schichten, die nicht übernommen werden, kommen in keiner anderen Warnung vor.

Mit `--report` steht je Grund und Schicht eine Zeile `verworfen` mit Kürzel (`schicht`), `dienste_historie`,
`stunden_historie`, `grund` und der SP5-Id der Schicht (`sp5_schicht`). `grund` ist `nicht_zugeordnet`,
`ignoriert`, `ausgeblendet`, `zu_lang`, `ohne_dienstzeit`, `unbekannte_schicht`, `abwesenheit`,
`anderer_eintrag`, `mitarbeiter_nicht_uebernommen` (auf übernommenen Schichten) oder
`mitarbeiter_und_schicht_nicht_uebernommen`. Stunden sind die Dienstzeit der Schicht je Dienst (bei Schichten ohne
Dienstzeit und unbekannten Schichten 0). Was zu tun ist:

- `nicht_zugeordnet`: in der Zuordnung unter `shifts` eintragen, wenn Dispofold die Schicht planen soll, sonst
  unter `ignoreShifts`. `ignoriert`: Absicht, nichts zu tun.
- `ausgeblendet`: in SP5 ausgeblendete Schicht mit Diensten in der Historie; in SP5 prüfen, ob die Schicht noch
  gebraucht wird (dann einblenden).
- `zu_lang`, `ohne_dienstzeit`: Zeiten der Schicht (SP5-Id) in SP5 korrigieren.
- `unbekannte_schicht`: Einträge in SP5 verweisen auf eine Schicht-Id, die es nicht gibt; in SP5 prüfen.
- `abwesenheit`, `anderer_eintrag`: Dienst und Abwesenheit oder zwei Einträge am selben Tag; in SP5 prüfen, wenn
  es viele sind.
- `mitarbeiter_nicht_uebernommen`: Personen, die arbeiten, aber nicht übernommen werden; Gruppen und
  `excludeGroups` prüfen (siehe [Bereiche aus der Historie](#bereiche-aus-der-historie)).

Dienste übernommener Schichten an Tagen mit ganztägiger Abwesenheit haben ihre eigene Warnung (`... an Tagen mit
ganztägiger Abwesenheit nicht übernommen`) und zählen hier nicht, ebenso Sonderdienste ohne Schicht.

### Wochenstunden nach CALCBASE

Die Zeile `Wochenstunden nach CALCBASE: ...; HRSWEEK weicht bei 15 davon ab` nennt nur eine Zahl. Mit
`--report` steht je gezählter Person eine Zeile `wochenstunden` mit der SP5-Id (`person`), `calcbase`,
`hrsweek`, dem Wert nach `CALCBASE` (`wochenstunden_calcbase`) und dem Wert im Datensatz (`wochenstunden`).
Verwendet wird immer der Wert nach `CALCBASE`, so wie SP5 das Soll rechnet. Ergibt er 0 Stunden, wird die
Person nicht übernommen und `wochenstunden` bleibt leer; die Zeile gibt es trotzdem, damit die Zahl der Zeilen
die Zahl der Warnung ist. Unter `CALCBASE` `0` sind Abweichungen normal; ist der Wert im Datensatz falsch, in SP5
die Felder der Grundlage korrigieren (`HRSDAY` und `WORKDAYS`, `HRSMONTH` oder `HRSTOTAL`).

### Korrekturdatei

Stammdaten, die in SP5 falsch oder veraltet sind, lassen sich in einer Korrekturdatei von Hand korrigieren.
`--corrections korrekturen.json` wendet sie bei jedem Export an, so gehen die Korrekturen bei einem neuen Export
nicht verloren. Format:

```json
{"format": "dispofold/corrections", "version": 1, "people": {
  "E12": {"note": "Freitext, wird nicht exportiert", "weeklyHours": 30.0, "areaIds": ["AN", "DR"],
          "shiftMix": {"categories": {"DAY": 0.9, "NIGHT": 0.1}, "areas": {"AN": 1.0}},
          "restrictions": [{"shiftId": "S5", "category": null, "weekday": 0, "kind": "ON_REQUEST"}]}}}
```

- Schlüssel ist die Id im Datensatz (`E<SP5-Id>`).
- Korrigierbar sind alle Personenfelder des Datenformats außer `id`, `externalId`, `firstName`, `lastName` und
  `shortName`: `weeklyHours`, `areaIds`, `hoursAccount`, `compRestBalance`, `familyRules`, `entryDate`,
  `exitDate`, `shiftMix`. Dazu `restrictions`, die Einschränkungen der Person.
- Ein vorhandenes Feld ersetzt den exportierten Wert ganz, ein fehlendes lässt ihn, wie er ist.
  `"shiftMix": null` entfernt den Mix. `restrictions` ersetzt alle Einschränkungen der Person (`[]` entfernt
  alle); die Einträge haben kein `personId`, sonst dieselben Felder wie im Datenformat.
- `note` ist nur für die Planung und kommt nie in den Datensatz.
- Die Korrekturen werden zuletzt angewendet, nach allem, was aus SP5 und der Historie abgeleitet wird. Die Zeilen
  `person` im Bericht zeigen deshalb die Werte vor der Korrektur.

Geprüft wird wie beim Import in Dispofold: Typen, Wertebereiche (Zahl größer 0 für `weeklyHours`, ganze Zahl für
`compRestBalance`, Datum `JJJJ-MM-TT`, Wochentag 0 bis 7), Anteile des Mix zwischen 0 und 1 mit Summe 1,
bekannte Bereichs- und Schicht-Ids, Austritt nicht vor Eintritt, Bereichsanteile des Mix nur für Bereiche der
Person. Entfernt eine Korrektur von `areaIds` einen Bereich, in dem der Mix einen Anteil hat, muss `shiftMix`
mitkorrigiert werden, sonst wird `areaIds` übersprungen.

Probleme halten den Export nicht an. Ein Feld mit ungültigem Wert oder unbekannter Id wird übersprungen
(`restrictions` als Ganzes), ebenso alle Felder einer Person, die nicht exportiert wird; dazu je eine Warnung.
Eine Zeile `Korrekturen: 3 Mitarbeiter korrigiert, 7 Felder angewendet, 1 Felder übersprungen` fasst zusammen.
Mit `--report` steht je Feld eine Zeile `korrektur` mit `person`, `feld`, `status` (`angewendet`,
`person_fehlt` oder `ungueltig`, dann mit dem Grund in `grund`), dem exportierten Wert (`wert_export`) und dem
korrigierten (`wert_korrektur`) als kurzer Text; Regeln und Einschränkungen als Anzahl. Werte aus der Datei, die
nicht gültig sind, und `note` stehen nie im Bericht oder in den Warnungen.

#### Version 2: Personen ausschließen, Bedarf setzen

Version 2 kann zusätzlich Personen ganz weglassen und den Bedarf eines Bereichs von Hand setzen, etwa für
einen frischen Datensatz, ohne vorher SP5 zu bereinigen (Ausgetretene ohne Austrittsdatum, Bereiche, in denen
die Historie ein falsches Bild gibt). Eine Datei der Version 1 wirkt genau wie bisher; `exclude` ist dort ein
unbekanntes Feld und `demand` wird nicht gelesen.

```json
{"format": "dispofold/corrections", "version": 2,
 "people": {"E12": {"exclude": true, "note": "ausgetreten, Austritt fehlt in SP5"},
            "E13": {"weeklyHours": 30.0}},
 "demand": {"LF": [{"days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"], "date": null,
                    "from": "05:20", "to": "05:20", "min": 1, "max": 1}]}}
```

- `"exclude": true` lässt die Person weg, mit allem, was sich auf sie bezieht: Einschränkungen, Historie,
  Abwesenheiten, Wünsche, fixe Dienste, Vormonatsdienste. Die anderen Felder der Person werden dann nicht
  angewendet. `false` oder fehlend: wie bisher. Ihre Dienste zählen weiter für den Bedarf aus der Historie.
  Die letzte Person des Datensatzes lässt sich nicht ausschließen.
- `demand.<Bereich>` ersetzt alle Bedarfsregeln des Bereichs. Die Einträge sind Bedarfsregeln des Datenformats
  ohne `areaId`; `[]` entfernt den Bedarf des Bereichs. Geprüft wird wie beim Import (`days` aus `MON` bis
  `SUN` und `HOL` oder ein `date`, `from` und `to` als Uhrzeit, `min` ganze Zahl ab 0, `max` null oder ab
  `min`). Ein unbekannter Bereich oder eine ungültige Regel überspringt den ganzen Eintrag dieses Bereichs.
  Die neuen Regeln stehen dort, wo die erste Regel des Bereichs stand.
- Im Bericht: `feld` `exclude` mit `status` `ausgeschlossen` (oder `person_fehlt`, `ungueltig`), und je
  Bereich eine Zeile ohne Person mit `feld` `demand:<Bereich>` und den Zahlen der Regeln vorher und nachher.
- Eine zweite Zeile fasst zusammen: `Korrekturen Version 2: 1 Mitarbeiter ausgeschlossen, Bedarf in 1 Bereichen
  ersetzt, 0 Bereiche übersprungen`.

Ein anderes `format`, eine `version` über 2 oder eine nicht lesbare Datei beenden den Export mit einem Fehler,
ebenso `demand`, das kein Objekt ist. Ohne `--corrections` ist der Datensatz derselbe wie ohne diese Option.

### Dienste auf ignorierten Schichten in den Planungsmonaten

In der Historie tun Dienste auf Schichten aus `ignoreShifts` nichts: Sie zählen weder für Stunden noch für
Bereiche, Schichten oder Bedarf. In den Planungsmonaten dagegen ist eine Person, die an einem Tag schon so
einen Dienst hat (Dienste auf ignorierten Schichten, etwa Schulung, Büro, Kurse), an dem Tag nicht frei. Der
Export macht daraus eine Abwesenheit der Person an diesem Tag mit der eigenen Abwesenheitsart `EXT`
("Sonstiger Dienst (ignorierte Schicht)", senkt das Soll, belastet das Stundenkonto nicht, keine Ersatzruhe).
Gelesen werden die Dienste wie für die Historie (Istplan `5MASHI` ohne `TYPE` 1, Sonderdienste `5SPSHI`,
Dienstzyklen), nur von übernommenen Mitarbeitern an Tagen ihrer Beschäftigung:

- Eine ganztägige Abwesenheit (`5ABSEN`) am selben Tag gewinnt; ebenso jede andere übernommene Abwesenheit
  des Tages (auch eine halbtägige), damit es je Person und Tag nur eine gibt.
- Hat der Tag einen Dienst auf einer zugeordneten Schicht, bleibt dieser Dienst maßgeblich und es entsteht
  keine Abwesenheit. Dienste der Planungsmonate kommen nicht in den Datensatz; Dispofold plant den Tag neu.
- Ein Nachtdienst sperrt nur den Tag, an dem er beginnt.
- Schichten, die weder zugeordnet noch in `ignoreShifts` sind, bleiben wie bisher weg (mit Warnung).

Die Art `EXT` steht nur im Datensatz, wenn es mindestens eine solche Abwesenheit gibt; sonst ist der Datensatz
genau wie ohne diese Regel. Gibt es in SP5 schon eine übernommene Abwesenheitsart `EXT`, bricht der Export mit
einer Meldung ab: dann `EXT` in `ignoreAbsences` eintragen oder die Regel abschalten. Abschalten lässt sie sich
in der Zuordnung mit `"ignoreShiftsAsAbsence": false`. Je Planungsmonat mit solchen Tagen meldet der Export:

```
Warnung: Dienste auf ignorierten Schichten im 2027-01: 3 Tage, 24.0 Stunden als Abwesenheit 'EXT' übernommen
```

Stunden sind wie oben die Dienstzeit der Schicht je Dienst. So rechnet Dispofold die Abwesenheit an: Wie jede
Art mit "senkt das Soll" zählt sie an einem Werktag (Montag bis Freitag, kein Feiertag) Wochenstunden / 5, an
Wochenenden und Feiertagen nichts; der Tag ist für die Planung gesperrt. Ein Schulungstag von 10 Stunden
senkt das Soll einer Person mit 40 Wochenstunden also um 8 Stunden, mit 20 Wochenstunden um 4 Stunden; ein
Schulungstag am Wochenende senkt es nicht. Die tatsächliche Dauer des Dienstes geht nicht ein.

### Schichten aus der Historie

Manche arbeiten nur bestimmte Schichten ihrer Bereiche (etwa nur Tagdienste), ohne dass SP5 eine
Einschränkung dafür hat. Mit Bereichen allein plant Dispofold sie in jeder Schicht des Bereichs ein. Der Export
zählt deshalb je Person und Schicht ihrer Bereiche (nach `--areas-from-history`) die Dienste der Historie und
meldet bei jedem Lauf (außer ohne Historie) nur Zahlen:

- je Bereich: wie viele Personen in mindestens einer Schicht des Bereichs weniger als N Dienste (ohne Option:
  keinen Dienst) hatten, und wie viele solche Paare aus Person und Schicht es gibt;
- wie viele Personen wegen zu kurzer Historie nicht bewertet werden und wie viele Paare schon eine
  Einschränkung aus SP5 haben.

`--shifts-from-history N` macht jede solche Schicht für die Person zur Einschränkung "nur auf Anfrage"
(`ON_REQUEST`, `weekday` 0 = alle Tage), nie zu "nie": Lücken in der Historie können Zufall sein. Dabei gilt:

- Hat `5RESTR` für Person und Schicht schon eine Zeile (gleich welcher Wochentag oder Grad), bleiben die
  Zeilen aus SP5 und es kommt nichts dazu.
- Nicht bewertet wird, wer in der Historie weniger als N Dienste in seinen Bereichen hatte oder weniger als
  3 Monate vor `--month` eingetreten ist.
- Wie `--areas-from-history` braucht die Option eine Historie (`--history-months` größer 0).

In Dispofold ist "nur auf Anfrage" die weiche Regel `onRequest` mit dem Standardgewicht 1000 je Dienst in
einer solchen Schicht (einstellbar unter Regeln und Gewichte). Nicht gedeckter Bedarf ist eine Stufe
darüber: Ist sonst niemand da, plant Dispofold die Person trotzdem ein. Die Einschränkungen erscheinen in der
Oberfläche nicht als eigene Liste. Wird jemand trotzdem so eingeplant, zeigt die Personenansicht beim Dienst
"Dienst ... nur auf Anfrage." Für Betrachter ist das ausgeblendet.

Empfehlung: zuerst ohne die Option mit `--report bericht.csv` laufen lassen und die Zeilen `schicht` ansehen,
dann `--shifts-from-history 3`. In den Zeilen `schicht` steht in `einschraenkung` `auf_anfrage` (ergänzt), `sp5`
(Einschränkung aus SP5 belassen), `zu_kurze_historie` (nicht bewertet) oder nichts.

### Bereiche, die nicht mehr gearbeitet werden

Eine Historie von 12 Monaten hält eine Person in einem Bereich "aktiv", den sie vor Monaten zuletzt gearbeitet
hat (etwa jemand, der für einen Bereich mit vielen Schichtvarianten berechtigt ist, ihn aber nicht mehr
arbeitet). `--shifts-from-history` greift dort nicht, wenn die Dienste von früher reichen. Solche Bereiche werden
deshalb "nur auf Anfrage": Die Person behält den Bereich (in `areaIds` und damit unter "Wer kann gefragt
werden?" in Dispofold), wird dort aber nie automatisch eingeplant.

- `--recent-months N`, Standard 3: Je übernommener Person zählen ihre letzten N **Monate mit Diensten** im
  Zeitraum der Historie, also Monate mit mindestens einem gezählten Dienst in irgendeiner Schicht mit Bereich
  (gezählt wie für Bereiche und Schichtmix). Monate ohne Dienst (lange Krankheit, Karenz) werden übersprungen;
  wer zurückkommt, wird an den Monaten vor der Abwesenheit gemessen.
- Ein Bereich der Person (`areaIds`) ohne Dienst in diesen N Monaten, aber mit Diensten davor in der Historie,
  bekommt für jede seiner Schichten eine Einschränkung "nur auf Anfrage" (`ON_REQUEST`, `weekday` 0). Hat die
  Person für eine Schicht schon eine Einschränkung für alle Tage (`weekday` 0, "nur auf Anfrage" oder das
  strengere "nie"), bleibt diese und es kommt nichts dazu; Einschränkungen für einzelne Wochentage bleiben neben
  der neuen stehen ("nie" gilt an seinem Tag weiter).
- Der Bereich fällt aus dem Schichtmix: Anteile je Kategorie und je Bereich werden ohne die Dienste in diesem
  Bereich neu gerechnet und ergeben wieder 1. Ob jemand einen Mix bekommt, entscheidet weiter die Zahl aller
  Dienste (`--mix-min-duties`); `dienste_historie` in der Zeile `person` zählt ebenfalls alle.
- Nicht bewertet wird, wer weniger als N Monate mit Diensten in der Historie hat oder in den N Monaten vor
  `--month` eingetreten ist. Bereiche ohne einen Dienst in der Historie bleiben den übrigen Regeln überlassen.
- Ohne Historie (`--history-months 0`) oder mit `--recent-months 0` ändert sich nichts; der Datensatz ist dann
  derselbe wie vor dieser Option.

Auf stderr steht eine Zeile mit der Zahl der betroffenen Personen, der Paare aus Person und Bereich, der
ergänzten Einschränkungen und der nicht bewerteten Personen. Mit `--report` steht je betroffener Person und
Bereich eine Zeile `nicht_mehr` mit `person`, `bereich`, den Diensten im Bereich in der Historie
(`dienste_historie`), `einschraenkung` `auf_anfrage` und dem Datum des letzten Dienstes im Bereich
(`letzter_dienst`). Der Bericht bleibt bei 29 Spalten.

Rückgängig für einzelne Personen: in der [Korrekturdatei](#korrekturdatei) `restrictions` der Person ohne die
ergänzten Einträge und, wenn gewünscht, `shiftMix` mit dem Bereich angeben (die Korrekturen werden zuletzt
angewendet), etwa im Korrektureditor dispofold-lupe im Einschränkungsraster des Bereichs. Für alle:
`--recent-months 0`.

### Schichtmix aus der Historie

Ziel des Grundimports ist, dass Dispofold jede Person so einplant, wie sie in SP5 tatsächlich arbeitet: Wer zu
90 % Tagdienste und zu 10 % Nachtdienste hat, soll in Dispofold ungefähr diese Mischung bekommen. Dafür
schreibt der Export je Person das optionale Feld `shiftMix`:

```json
"shiftMix": {"categories": {"DAY": 0.9, "NIGHT": 0.1}, "areas": {"AN": 0.7, "DR": 0.3}}
```

- Gezählt werden die Dienste der Historie wie für die Historieneinträge: nur Schichten mit Bereich (nicht
  `ignoreShifts`), Sonderdienste wie oben, an Tagen mit ganztägiger Abwesenheit kein Dienst.
- `categories`: Anteil je Schichtkategorie (`EARLY`, `DAY`, `LATE`, `NIGHT`) an allen gezählten Diensten der
  Person. Es gilt die Kategorie, die der Export für die Schicht in `shifts[].category` schreibt (aus der
  Zuordnung oder aus der Uhrzeit, siehe [Zuordnungsdatei](#zuordnungsdatei)).
- `areas`: Anteil je Bereich, nur über die Dienste in den Bereichen der Person (`areaIds`, nach
  `--areas-from-history`, ohne Bereiche, die sie nicht mehr arbeitet; siehe
  [Bereiche, die nicht mehr gearbeitet werden](#bereiche-die-nicht-mehr-gearbeitet-werden)). Dienste in anderen Bereichen fallen dafür weg und die übrigen Anteile werden neu
  auf 1 verteilt, weil Dispofold nur Bereiche der Person annimmt. Hat jemand nur Dienste in fremden Bereichen,
  ist `areas` leer.
- Anteile auf drei Stellen gerundet, Anteile 0 weggelassen; den Rundungsrest bekommt der größte Anteil, so dass
  jede Liste genau 1 ergibt.
- Nur wer in der Historie mindestens 10 Dienste hat (`--mix-min-duties N`), bekommt einen Mix; bei allen
  anderen fehlt das Feld. Ohne Historie bekommt niemand einen Mix. `--no-shift-mix` schaltet den Mix ab.
- Auf stderr steht nur die Zahl der Personen mit Mix und die der Personen unter der Mindestzahl.

Dispofold nimmt den Mix als Vorgabe für die Planung; die Planung kann ihn dort je Person ändern oder löschen.
Dispofold lehnt beim Import einen Mix ab (Fehlercode `import.badShiftMix`), wenn ein Anteil außerhalb von 0
bis 1 liegt, eine nicht leere Liste nicht innerhalb von 0,01 auf 1 kommt oder ein Bereich nicht zur Person
gehört. Der Datensatz bleibt Version 2; ältere Datensätze ohne das Feld sind weiter gültig.

**Durchsehen aller Personen:** `--report bericht.csv` schreibt je übernommener Person eine Zeile `person` mit
SP5-Id (`person`), Diensten in der Historie (`dienste_historie`), `wochenstunden`, `bereiche` (durch Leerzeichen
getrennt), Mix je Kategorie (`mix_kategorien`, z. B. `DAY 0.900, NIGHT 0.100`) und je Bereich (`mix_bereiche`)
und der Zahl der Einschränkungen "nur auf Anfrage" (`auf_anfrage`, aus SP5, aus `--shifts-from-history` und
aus `--recent-months`).
Ohne Mix (unter der Mindestzahl oder mit `--no-shift-mix`) bleiben die beiden Mix-Spalten leer. So lassen sich
alle Personen einmal in einer Tabellenkalkulation durchsehen (nach `person` filtern); in Dispofold sind dann
nur die Ausnahmen zu korrigieren.

### Bedarf aus der Historie

Der Bedarf in SP5 (`5SHDEM`) taugt in vielen Betrieben nicht als Planungsziel:

- Derselbe Bedarf ist für mehrere Gruppen eingetragen (übergeordnete und Untergruppe oder Gruppen ohne Bezug
  zueinander); summiert ergibt das ein Vielfaches.
- Mehrere Schichtvarianten einer Position tragen je `MIN` 1, gemeint ist aber eine davon; summiert werden es
  so viele Personen wie Varianten.
- Bereiche, die jeden Tag besetzt sind, haben oft gar keinen Bedarf in SP5.

So entsteht Bedarf für mehrere Personen, wo eine arbeitet, und kein Bedarf, wo mehrere arbeiten. Verlässlich
ist dagegen die tatsächliche Besetzung in der Historie. Deshalb nimmt der Export den Bedarf standardmäßig aus
den Diensten der Historie (`--history-months`, dieselben Dienste wie für die Historie: Istplan `5MASHI` ohne
`TYPE` 1, Sonderdienste `5SPSHI`, Dienstzyklen; an Tagen mit ganztägiger Abwesenheit gilt kein Dienst):

- Es zählen die Dienste aller Personen in Schichten mit Bereich (nicht `ignoreShifts`, nicht ausgeblendet),
  auch von Personen, die nicht übernommen werden: ausgetreten, noch nicht eingetreten, ausgeblendet, ohne
  Bereich oder ohne Wochenstunden, oder mit `exclude` aus der Korrekturdatei entfernt. Ihre Dienste mussten
  andere übernehmen, sonst fehlt der Bedarf genau dort, wo jemand gegangen ist. Nicht zählen nur Personen in
  `excludeGroups`: Deren Bedarf soll wegfallen.
- Je Bereich und Tagestyp (`MON` bis `SUN`, `HOL` für die Feiertage, die auch für die Historie gelten; ein
  Feiertag zählt nur als `HOL`) wird der Tag an jeder Beginn- und Endzeit der im Bereich gearbeiteten Schichten
  geteilt. Für jeden Abschnitt zählt der Export an jedem Tag dieses Typs im Zeitraum, wie viele gleichzeitig im
  Dienst waren. Jeder Tag zählt, auch einer ohne Dienst im Bereich (mit 0).
- Ein Dienst zählt für den Tag, an dem er beginnt; ein Nachtdienst reicht in den Folgetag. So kennt Dispofold
  Bedarfsfenster: Ein Fenster gehört zum Tag, an dem es beginnt, und `to <= from` heißt, es endet am Folgetag.
- `min` des Abschnitts ist der Median dieser Zahlen, ein halber Wert wird aufgerundet (1, 1, 2, 2 ergibt 2).
  `max` ist das 90. Perzentil derselben Zahlen (Nearest-Rank: die Zahl auf Rang aufgerundet 0,9 mal Anzahl der
  Tage in der sortierten Liste, also immer eine der gezählten Zahlen; 1, 1, 2, 3 ergibt 3) und nie kleiner als
  `min`. So bleibt eine Obergrenze wie "höchstens einer gleichzeitig" erhalten. Benachbarte Abschnitte werden
  nur zu einem Fenster (höchstens 24 Stunden), wenn `min` und `max` gleich sind. Abschnitte mit `max` 0 fallen
  weg; ein Abschnitt mit `min` 0 und `max` ab 1 bleibt als reine Obergrenze. Wochentage mit gleichen Fenstern werden eine Regel, dazu Regeln `HOL`; Regeln mit
  Datum gibt es nicht. Ist ein Bereich an Feiertagen im Median nie besetzt, an anderen Tagen aber schon, bekommt
  er eine Regel `HOL` mit `min` 0: Ohne `HOL`-Regel nähme Dispofold an Feiertagen die Wochentagsregeln.
- Nach Mitternacht beginnt kein Dienst mehr, die Besetzung kann dort nur sinken. Diesen Teil schreibt der
  Export als Fenster, die alle zur letzten Grenze vor Mitternacht beginnen und sich addieren (Dispofold zählt
  überlappende Fenster zusammen, `min` und `max` je für sich). Die Summen von `min` und `max` über die Fenster
  geben die Abschnitte genau wieder. Nur wenn `max` von einem Schritt zum nächsten weniger fällt als `min`,
  ginge das nicht (ein Fenster braucht `min` höchstens `max`); dann wird `min` des früheren Abschnitts gesenkt,
  `max` bleibt genau. Beispiel: Tagdienst 07:00 bis 19:00 und zwei Nachtdienste 19:00 bis 07:00 und
  22:00 bis 06:00 ergeben `07:00`-`07:00` mit 1 und `22:00`-`06:00` mit 1.

`--demand sp5` nimmt wie bisher den Bedarf aus SP5 (`5SHDEM` summiert, `5SPDEM` als Regeln mit Datum), etwa
zum Vergleich. Ohne Historie (`--history-months 0`) ist `--demand sp5` nötig, sonst endet der Export mit
einem Fehler.

Bei jedem Lauf mit Historie (gleich mit welchem `--demand`) meldet der Export je Bereich nur Zahlen: je
Tagestyp die Spitze des Bedarfs aus der Historie gegen die Spitze des SP5-Bedarfs (beide als höchste
gleichzeitige Besetzung eines Kalendertags, samt dem Ende der Nachtdienste des Vortags; aus der Historie als
`min-max`, etwa `MON 1-2/8`), wie viele Dienste gezählt wurden, davon wie viele von Personen, die nicht
übernommen werden, und wie viele nicht gezählt wurden (ausgeschlossene Gruppe). Mit `--report` stehen dieselben
Zahlen je Bereich und Tagestyp in den Zeilen `bedarf`: `dienste_historie` gezählte Dienste, `spitze_historie`
(min), `spitze_historie_max`, `spitze_sp5`, `dienste_nicht_uebernommen` (nicht gezählt, ausgeschlossene
Gruppe) und `dienste_nicht_uebernommen_gezaehlt` (gezählt, Person nicht im Datensatz).

Die Ausgabedatei darf nicht im SP5-Ordner liegen. Warnungen und eine Zusammenfassung erscheinen auf der
Konsole (stderr). Bei einem Fehler endet das Programm mit Exit-Code 1 und schreibt keine Datei.
Fehler sind unter anderem: eine Tabelle ist nicht lesbar, beschädigt oder unvollständig (die Zahl der
Datensätze im Dateikopf passt nicht zur Datei), eine Pflichttabelle (`5EMPL`, `5SHIFT`, `5GROUP`, `5GRASG`) ist
leer, keine Schicht oder kein Mitarbeiter wird übernommen, ein Mitarbeiter hat ein unbekanntes `CALCBASE`. Texte, die nicht UTF-16 sind, werden mit der Codepage
aus dem Dateikopf gelesen (z. B. cp850, cp1252); eine unbekannte Codepage ist ein Fehler.

Die erzeugte Datei in Dispofold unter **Import/Export** mit **"Datensatz importieren"** hochladen.
Sie enthält Namen der Mitarbeiter und gehört daher nicht in E-Mails oder Repositories.

## Zuordnungsdatei

Die Zuordnung ist eine JSON-Datei; `mapping.example.json` ist ein Beispiel und muss für den echten
Datenbestand geprüft und angepasst werden (eine Kopie anlegen, z. B. `mapping.json`).

**Muster:** Überall, wo ein Muster steht (`match` oder Listeneinträge), gilt:

- ohne Präfix ein Platzhaltermuster, Groß-/Kleinschreibung zählt: `*` beliebig viele Zeichen, `?` genau
  ein Zeichen, `[abc]` eines der Zeichen. `A*` passt auf `A1`, `A2`, `A-Test` usw.
- mit Präfix `re:` ein regulärer Ausdruck, der am Anfang des Werts passen muss, z. B.
  `re:^(R1|R3|RX/.*)$`. In JSON ist der Rückstrich doppelt zu schreiben (`\\.`).

Bei Listen von Regeln gilt die **erste passende** Regel. Spezielle Regeln daher vor allgemeine setzen.

| Schlüssel | Inhalt | Bedeutung |
|---|---|---|
| `areas` | Liste `{id, name, color}` | Planungsbereiche, so wie sie im Datensatz erscheinen. Die Reihenfolge ist auch die Reihenfolge der `areaIds` einer Person. Pflicht. |
| `shifts` | Liste `{match, areaId, category?, unpopular?}` | Ordnet SP5-Schichten über ihr **Kürzel** (`5SHIFT.SHORTNAME`) einem Bereich zu. `category` (`EARLY`, `DAY`, `LATE`, `NIGHT`) überschreibt die aus den Zeiten abgeleitete Kategorie; `unpopular` (true/false) überschreibt die Vorgabe "Nachtdienste sind unbeliebt". Pflicht. |
| `ignoreShifts` | Liste von Mustern | Schichten, die bewusst nicht geplant werden (z. B. Servicetelefon, Verwaltung). Sie erzeugen keine Warnung; ihr Bedarf und ihre Dienste fallen weg, in den Planungsmonaten werden ihre Dienste aber Abwesenheiten (siehe [Dienste auf ignorierten Schichten in den Planungsmonaten](#dienste-auf-ignorierten-schichten-in-den-planungsmonaten)). |
| `groups` | Liste `{match, areaIds}` | Ordnet SP5-Gruppen über ihren **Namen** (`5GROUP.NAME`) Bereichen zu. Eine Person darf in allen Bereichen ihrer Gruppen eingesetzt werden (Qualifikation). Mehrere Gruppen dürfen auf denselben Bereich zeigen. Pflicht. |
| `ignoreGroups` | Liste von Mustern | Gruppen ohne Bedeutung für die Qualifikation (Teams, "alle"). Sie erzeugen keine Warnung. |
| `excludeGroups` | Liste von Mustern | Wer in einer dieser Gruppen ist, wird **nicht** übernommen, auch wenn andere Gruppen Bereiche geben (Ehrenamt, Servicetelefon, Verwaltung). Ihr Bedarf fällt ebenfalls weg. |
| `compensatoryRestAbsences` | Liste von Mustern | Abwesenheitskürzel (`5LEAVT.SHORTNAME`), die Ersatzruhe sind. |
| `ignoreAbsences` | Liste von Mustern | Abwesenheitsarten, die nicht übernommen werden. |
| `ignoreShiftsAsAbsence` | true oder false | Dienste auf `ignoreShifts`-Schichten in den Planungsmonaten als Abwesenheit `EXT` übernehmen. Standard `true`. |

Die Zuordnung wird vor dem Lesen des SP5-Ordners vollständig geprüft. Fehlende Pflichtschlüssel, unbekannte
Schlüssel (Tippfehler), falsche Typen, ungültige `re:`-Muster und Verweise auf Bereiche, die nicht in `areas`
stehen, sind Fehler; die Meldung nennt alle Probleme mit Liste und Eintragsnummer.

Untergruppen (`5GROUP.SUPERID`) ohne eigene Regel erben die Regel der nächsten übergeordneten Gruppe
(`groups`, `ignoreGroups` oder `excludeGroups`).

## Was übernommen wird

- **Personen:** wer nicht ausgeblendet ist (`HIDE`), in einem der Planungsmonate beschäftigt ist
  (`EMPSTART`/`EMPEND`), in keiner `excludeGroups`-Gruppe ist, Wochenstunden hat und über `groups` (oder
  `--areas-from-history`) mindestens einen Bereich bekommt.
  Wochenstunden so, wie SP5 das Soll rechnet, nach `CALCBASE`: `0` Stunden je Tag (`HRSDAY`) mal Arbeitstage
  Montag bis Sonntag aus `WORKDAYS`, `1` `HRSWEEK`, `2` `HRSMONTH` × 12 / 52, `3` `HRSTOTAL` verteilt auf die
  Wochen von `EMPSTART` bis `EMPEND` (ohne `EMPEND` wird die Person nicht übernommen). Gerundet auf zwei
  Stellen. Unter `0` ist `HRSWEEK` oft veraltet und wird nicht gelesen; eine Zeile nennt, wie viele Personen
  welche Grundlage haben und bei wie vielen `HRSWEEK` abweicht (nur Zahlen). Id `E<SP5-Id>`, `externalId` ist die SP5-Id. `EMPSTART`/`EMPEND` werden
  `entryDate`/`exitDate`; Dispofold verplant Tage außerhalb nicht und rechnet das Soll anteilig. Mit genug
  Diensten in der Historie kommt der Schichtmix dazu (siehe [Schichtmix aus der Historie](#schichtmix-aus-der-historie)).
- **Schichten:** Id `S<SP5-Id>`, Kürzel, Name und Farbe aus SP5. Ausgeblendete nur, wenn sie in Gebrauch
  sind (`--hidden-shifts-months`). Bei Zeiten je Wochentag gilt die
  häufigste Zeit. Schichten über 12 Stunden (die Standardregel `maxDutyHours` von Dispofold) werden nicht
  übernommen, weil Dispofold sonst den ganzen Import ablehnt. Ist `maxDutyHours` in Dispofold kleiner
  eingestellt, lehnt Dispofold längere Schichten trotzdem ab (Fehlercode `import.shiftTooLong`).
- **Einschränkungen** (`5RESTR`): `RESTRICT` 2 = nie, 1 = nur auf Anfrage, 0 = keine. Mit `--shifts-from-history`
  und `--recent-months` kommen "nur auf Anfrage" aus der Historie dazu.
- **Abwesenheiten** (`5LEAVT`, `5ABSEN`): je Person und Tag die erste, nur an Tagen der Beschäftigung.
  "Senkt das Soll" (`reducesTarget`), wenn SP5 die Art anrechnet (`CHARGETYP` nicht 0); "belastet das
  Stundenkonto" (`chargesAccount`), wenn sie zusätzlich von den Ist- oder Überstunden abgezogen wird
  (`DEDUCTACT` oder `DEDUCTOVT`, z. B. Zeitausgleich). Bitte für den echten Bestand in Dispofold prüfen.
  Dazu kommen Dienste auf `ignoreShifts`-Schichten in den Planungsmonaten als Art `EXT` (siehe
  [Dienste auf ignorierten Schichten in den Planungsmonaten](#dienste-auf-ignorierten-schichten-in-den-planungsmonaten)).
- **Bedarf:** standardmäßig aus der tatsächlichen Besetzung in der Historie (siehe
  [Bedarf aus der Historie](#bedarf-aus-der-historie)). Mit `--demand sp5` aus `5SHDEM` und `5SPDEM`: je Bereich
  in Zeitfenster umgerechnet, Feiertage als `HOL`, Sonderbedarf und abweichende Tage als Regeln mit Datum.
- **Feiertage** aus `5HOLID` (nur ganztägige); fehlen sie, die österreichischen gesetzlichen Feiertage.
- **Vormonatsdienste** der letzten 7 Tage und **Historie** der Monate vor dem ersten Planungsmonat (Standard 12, `--history-months`) aus `5SPSHI`, `5MASHI`
  (Istplan) und den Dienstzyklen. Ein Sonderdienst (`5SPSHI`) mit Schicht ersetzt den Dienst des Tages, gleich
  welcher `TYPE` und auch dann, wenn seine Schicht nicht übernommen wird (wie in sp5lib; die Bedeutung von `TYPE` ist nicht dokumentiert, daher eine Warnung). An Tagen
  mit ganztägiger Abwesenheit (`5ABSEN`, `INTERVAL` 0) gilt kein Dienst; SP5 zeigt beides als Konflikt, der
  Datensatz kennt keine Abwesenheiten außerhalb des Planungsmonats. Historieneinträge (je Person und Monat, in dem sie beschäftigt war, auch ohne Dienst mit 0 Stunden, oder mit Diensten oder Ersatzruhe) haben `source` `import`;
  `compRestUsed` zählt ganztägige Ersatzruhe-Abwesenheiten, `compRestEarned` die Feiertagsdienste, die nicht auf
  einen Sonntag fallen (Standardregel von Dispofold).
- **Nicht aus SP5:** Stundenkonto und offene Ersatzruhetage (beide 0, in der Historie `null`),
  Familienwünsche, Wünsche, fixe Dienste und die Regeln (Ruhezeiten, Zeitzone, Durchrechnung). Diese pflegt die
  Planung in Dispofold.

## Warnungen

Warnungen nennen nur SP5-Ids, Kürzel und Anzahlen, nie Namen. Die wichtigsten:

| Warnung | Bedeutung und Abhilfe |
|---|---|
| `Schicht ohne Bereich ignoriert: SP5-Id 31, Kürzel 'X-1'` | Kein Muster in `shifts` passt. Muster in `shifts` oder `ignoreShifts` ergänzen. |
| `Schicht ohne Dienstzeit ignoriert: ...` | Die Schicht hat in SP5 keine Zeit `HH:MM-HH:MM`. |
| `Schicht 'X' (SP5-Id 3) hat je Wochentag andere Zeiten; verwendet wird ...` | Nur die häufigste Zeit wird übernommen. |
| `Schicht 'X' (SP5-Id 3) dauert mehr als 12 Stunden (...) und wird nicht übernommen` | Zeiten in SP5 prüfen. |
| `Ausgeblendete Schichten: 2 übernommen, weil in Gebrauch (...), 3 nicht übernommen` | Nur wenn es ausgeblendete Schichten gibt; welche: Zeilen `ausgeblendet` im Bericht (siehe [Ausgeblendete Schichten in Gebrauch](#ausgeblendete-schichten-in-gebrauch)). |
| `Gruppe ohne Zuordnung: SP5-Id 7, Kürzel 'G7'` | Gruppe in `groups`, `ignoreGroups` oder `excludeGroups` aufnehmen. |
| `17 Mitarbeiter nicht übernommen (Grund)` | Gründe: ausgeblendet, nicht beschäftigt im Monat, ausgeschlossene Gruppe, ohne Bereich. Zahl mit dem erwarteten Personalstand vergleichen. |
| `Mitarbeiter E12 hat keine Wochenstunden (CALCBASE 1) und wird nicht übernommen` | Die Stunden der Grundlage in SP5 nachtragen (`HRSDAY` und `WORKDAYS`, `HRSWEEK`, `HRSMONTH` oder `HRSTOTAL`). |
| `Wochenstunden nach CALCBASE: 0 (Tag) 40, 1 (Woche) 12, ...; HRSWEEK weicht bei 25 davon ab` | Immer vorhanden. Viele Abweichungen unter `0` sind normal: SP5 rechnet dort nicht mit `HRSWEEK`. Wer: Zeilen `wochenstunden` im Bericht (siehe [Wochenstunden nach CALCBASE](#wochenstunden-nach-calcbase)). |
| `Mitarbeiter E12 hat Gesamtstunden (CALCBASE 3) ohne abgeschlossene Beschäftigung (EMPSTART bis EMPEND) und wird nicht übernommen` | In SP5 das Austrittsdatum eintragen oder eine andere Grundlage wählen. |
| `3 Mitarbeiter nur teilweise im Monat beschäftigt; ...` | Ein- oder Austritt im Monat. |
| `Bereich AN: 2 Mitarbeiter mit Diensten in den 12 Monaten davor ohne den Bereich aus Gruppen, 5 mit ...` | Immer vorhanden (außer ohne Historie); siehe [Bereiche aus der Historie](#bereiche-aus-der-historie). |
| `Bereiche aus Diensten (mindestens 3): ...` | Nur mit `--areas-from-history`: wie viele Bereiche ergänzt und wie viele Personen dadurch übernommen wurden. |
| `Schichten im Bereich AN: 4 Mitarbeiter mit keinem Dienst in mindestens einer Schicht des Bereichs ...` | Immer vorhanden (außer ohne Historie); siehe [Schichten aus der Historie](#schichten-aus-der-historie). |
| `Schichten aus Diensten: 3 Mitarbeiter mit zu kurzer Historie ... nicht bewertet, 20 Paare ... mit Einschränkung aus SP5 belassen` | Immer vorhanden (außer ohne Historie). |
| `Schichten aus Diensten (mindestens 3): 12 Einschränkungen 'nur auf Anfrage' ergänzt` | Nur mit `--shifts-from-history`. |
| `Bereiche nicht mehr gearbeitet (kein Dienst in den letzten 3 Monaten mit Diensten): 2 Mitarbeiter, 2 Paare ..., 9 Einschränkungen 'nur auf Anfrage' ergänzt; 14 Mitarbeiter ... nicht bewertet` | Immer vorhanden außer ohne Historie oder mit `--recent-months 0`; welche: Zeilen `nicht_mehr` im Bericht (siehe [Bereiche, die nicht mehr gearbeitet werden](#bereiche-die-nicht-mehr-gearbeitet-werden)). |
| `Schichtmix aus der Historie: 120 Mitarbeiter mit Mix, 14 mit weniger als 10 Diensten in den 12 Monaten davor ohne Mix` | Immer vorhanden außer mit `--no-shift-mix`; siehe [Schichtmix aus der Historie](#schichtmix-aus-der-historie). |
| `Bedarf im Bereich AN, Spitze je Tagestyp aus der Historie/aus SP5: MON 3-4/8, ...; 1200 Dienste ... gezählt, davon 40 von Mitarbeitern, die nicht übernommen werden; 17 nicht gezählt (ausgeschlossene Gruppe)` | Immer vorhanden (außer ohne Historie); siehe [Bedarf aus der Historie](#bedarf-aus-der-historie). Eine große Abweichung ist bei SP5-Bedarf zu erwarten. Viele nicht gezählte Dienste: `excludeGroups` prüfen. |
| `Schicht 'S1' nicht zugeordnet: 120 Dienste, 960.0 Stunden ...` oder `Schicht 'X2' ignoriert: ...` | Je Schicht ohne Bereich mit Diensten in der Historie; siehe [Dienste auf Schichten ohne Bereich](#dienste-auf-schichten-ohne-bereich). Nicht zugeordnet: in `shifts` eintragen, wenn Dispofold sie planen soll. Ignoriert ist Absicht. |
| `Schichten ohne Bereich: 2 Schichten, 160 Dienste, 1280.0 Stunden ...` | Immer vorhanden (außer ohne Historie): die Summe dazu. |
| `Verworfene Dienste 2026-12-01..2026-12-31, <Grund>: ...` | Immer vorhanden (außer ohne Historie): verworfene Dienste der Historie je Grund; siehe [Verworfene Dienste der Historie](#verworfene-dienste-der-historie). |
| `Bedarf aus der Historie: Median ...` oder `Bedarf aus SP5 ...` | Immer vorhanden (außer ohne Historie): woher der Bedarf im Datensatz kommt. |
| `Stundenkonto und offene Ersatzruhetage werden aus SP5 nicht übernommen (0)` | Immer vorhanden; Werte in Dispofold pflegen. |
| `Korrekturen: 3 Mitarbeiter korrigiert, 7 Felder angewendet, 1 Felder übersprungen` | Nur mit `--corrections`; siehe [Korrekturdatei](#korrekturdatei). |
| `Korrektur E12 weeklyHours übersprungen: <Grund>` oder `Korrektur für E12 übersprungen: Mitarbeiter nicht exportiert` | Die Korrektur in der Datei berichtigen oder entfernen. |
| `Korrekturen Version 2: 1 Mitarbeiter ausgeschlossen, Bedarf in 2 Bereichen ersetzt, 0 Bereiche übersprungen` | Nur mit einer Korrekturdatei der Version 2. |
| `Korrektur demand:AN übersprungen: <Grund>` | Die Bedarfsregeln des Bereichs in der Datei berichtigen. |
| `Tabelle 5XXX fehlt, wird als leer behandelt` | Falscher Ordner oder unvollständige Kopie. |
| `... Einschränkungen nur für Feiertage nicht übernommen` | Das Datenformat kennt keine Feiertagseinschränkung. |
| `... Einschränkungen ohne gültigen Grad (RESTRICT) als 'nie' übernommen` | Grad in SP5 prüfen. |
| `... weitere Abwesenheiten am selben Tag ignoriert` | Je Person und Tag zählt die erste Abwesenheit. |
| `Dienste auf ignorierten Schichten im 2027-01: 3 Tage, 24.0 Stunden als Abwesenheit 'EXT' übernommen` | Je Planungsmonat mit solchen Tagen; siehe [Dienste auf ignorierten Schichten in den Planungsmonaten](#dienste-auf-ignorierten-schichten-in-den-planungsmonaten). |
| `Keine Feiertage in SP5 (5HOLID); ...` | Die österreichischen Feiertage werden verwendet. |
| `... halbe Feiertage (5HOLID INTERVAL) nicht als Feiertag übernommen` | Halbe Feiertage zählen nur für den SP5-Bedarf (`--demand sp5`) als Feiertag. |
| `Bereich X: am YYYY-MM-DD kein Bedarf in SP5, es gilt der Wochentagsbedarf` | Nur mit `--demand sp5`: SP5 hat an dem Tag keinen Bedarf; in Dispofold prüfen. |
| `... Dienste ... mit nicht zugeordneter Schicht ignoriert` | Einträge übernommener Mitarbeiter auf Schichten, die nicht im Datensatz sind, je für die Historie und die Vormonatsdienste; die Gründe stehen in [Verworfene Dienste der Historie](#verworfene-dienste-der-historie). |
| `... Dienste ... an Tagen mit ganztägiger Abwesenheit nicht übernommen` | Dienst und Abwesenheit am selben Tag; in SP5 prüfen. |
| `... Sonderdienste ... mit TYPE ungleich 0 als Dienst übernommen` | Prüfen, ob diese Sonderdienste wirklich gearbeitet wurden. |
| `... Sonderdienste ... ohne Schicht nicht übernommen` | Sonderdienste mit eigenen Zeiten haben keine Schicht im Datensatz. |

Beim ersten Einsatz mit echten Daten sollte die Liste leer sein bis auf den Hinweis zum Stundenkonto und
die Zähler der nicht übernommenen Mitarbeiter.

## Entwicklung

Tests verwenden nur erfundene Daten (`tools/generator.py`, `tools/sp5writer.py` schreibt daraus einen
synthetischen SP5-Ordner). Echte SP5-Daten gehören nie ins Repository.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
sh scripts/test-check-repo.sh
sh scripts/check-repo.sh
```

`tests/dispofold/` enthält Kopien des JSON-Schemas und der Annahme- und Ablehnungsbeispiele von Dispofold
(Commit in `tests/dispofold/SOURCE.md`); `tests/contract.py` bildet die Importprüfung von Dispofold Regel für
Regel nach und wird an den erwarteten Fehlercodes der Ablehnungsbeispiele geprüft. Jeder Export in den Tests
muss Schema und Prüfung ohne Fehler und ohne Warnung bestehen. `tests/test_vendor_sample.py` lädt die Beispieldatenbank des
SP5-Herstellers herunter und wird ohne Netz oder ohne `innoextract` übersprungen.

## Datenformat

Das Zielformat ist in `docs/datenformat.md` beschrieben (Kopie aus Dispofold).

## Lizenz

MIT-Lizenz für das ganze Repository, Copyright (c) 2026 Matthias Schabhüttl, siehe `LICENSE`. Das gilt auch für
`vendor/sp5lib` (`vendor/sp5lib/LICENSE`).
