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
| `--report DATEI` | CSV mit SP5-Ids und Bedarfsvergleich (siehe unten) |

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
pruefung;person;bereich;schicht;dienste_historie;bereich_aus_gruppen;einschraenkung;tagestyp;spitze_historie;spitze_sp5;dienste_nicht_uebernommen
bedarf;;AN;;412;;;MON;3;8;17
bereich;E12;AN;;41;ja;;;;;
schicht;E12;AN;AN-N;0;;auf_anfrage;;;;
```

Zeilen `bereich` je Person und Bereich, den sie aus Gruppen hat oder in dem sie gearbeitet hat
(`bereich_aus_gruppen` ja oder nein); Zeilen `schicht` je Person und Schicht ihrer Bereiche (siehe
[Schichten aus der Historie](#schichten-aus-der-historie)); Zeilen `bedarf` je Bereich und Tagestyp ohne Person
(siehe [Bedarf aus der Historie](#bedarf-aus-der-historie)). Die Datei enthält keine Namen, nur SP5-Ids und
Kürzel, liegt neben dem Datensatz und ist wie dieser per `.gitignore` ausgeschlossen.

Empfehlung für den ersten Lauf: `--areas-from-history 3 --report bericht.csv`, dann den Bericht mit der
Zuordnung vergleichen und die Zuordnung oder die Gruppen in SP5 korrigieren.

Ausbildungsdienste zählen nicht, wenn ihre Schichten wie vorgesehen in `ignoreShifts` stehen: Sie haben dann
keinen Bereich. Bewusst nicht aus der Historie abgeleitet werden Wünsche und Einschränkungen nach Art "nie":
Gewohnheiten der Vergangenheit sind keine Regeln.

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

- Es zählen nur Dienste von Personen, die übernommen werden, in Schichten mit Bereich (nicht `ignoreShifts`,
  nicht `excludeGroups`): Nur sie kann Dispofold einplanen.
- Je Bereich und Tagestyp (`MON` bis `SUN`, `HOL` für die Feiertage, die auch für die Historie gelten; ein
  Feiertag zählt nur als `HOL`) wird der Tag an jeder Beginn- und Endzeit der im Bereich gearbeiteten Schichten
  geteilt. Für jeden Abschnitt zählt der Export an jedem Tag dieses Typs im Zeitraum, wie viele gleichzeitig im
  Dienst waren. Jeder Tag zählt, auch einer ohne Dienst im Bereich (mit 0).
- Ein Dienst zählt für den Tag, an dem er beginnt; ein Nachtdienst reicht in den Folgetag. So kennt Dispofold
  Bedarfsfenster: Ein Fenster gehört zum Tag, an dem es beginnt, und `to <= from` heißt, es endet am Folgetag.
- `min` des Abschnitts ist der Median dieser Zahlen, ein halber Wert wird aufgerundet (1, 1, 2, 2 ergibt 2);
  `max` bleibt leer. Benachbarte Abschnitte mit gleichem Wert werden ein Fenster (höchstens 24 Stunden),
  Abschnitte mit 0 fallen weg. Wochentage mit gleichen Fenstern werden eine Regel, dazu Regeln `HOL`; Regeln mit
  Datum gibt es nicht. Ist ein Bereich an Feiertagen im Median nie besetzt, an anderen Tagen aber schon, bekommt
  er eine Regel `HOL` mit `min` 0: Ohne `HOL`-Regel nähme Dispofold an Feiertagen die Wochentagsregeln.
- Nach Mitternacht beginnt kein Dienst mehr, die Besetzung kann dort nur sinken. Diesen Teil schreibt der
  Export als Fenster, die alle zur letzten Grenze vor Mitternacht beginnen und sich addieren (Dispofold zählt
  überlappende Fenster zusammen). Beispiel: Tagdienst 07:00 bis 19:00 und zwei Nachtdienste 19:00 bis 07:00 und
  22:00 bis 06:00 ergeben `07:00`-`07:00` mit 1 und `22:00`-`06:00` mit 1.

`--demand sp5` nimmt wie bisher den Bedarf aus SP5 (`5SHDEM` summiert, `5SPDEM` als Regeln mit Datum), etwa
zum Vergleich. Ohne Historie (`--history-months 0`) ist `--demand sp5` nötig, sonst endet der Export mit
einem Fehler.

Bei jedem Lauf mit Historie (gleich mit welchem `--demand`) meldet der Export je Bereich nur Zahlen: je
Tagestyp die Spitze des Bedarfs aus der Historie (höchster Wert eines Abschnitts) gegen die Spitze des
SP5-Bedarfs, wie viele Dienste gezählt wurden und wie viele nicht, weil die Person nicht übernommen wird
(ausgeschlossene Gruppe, ausgeblendet, ausgetreten usw.). Mit `--report` stehen dieselben Zahlen je Bereich und
Tagestyp in den Zeilen `bedarf`: `dienste_historie` gezählte Dienste, `spitze_historie`, `spitze_sp5` und
`dienste_nicht_uebernommen`.

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
| `ignoreShifts` | Liste von Mustern | Schichten, die bewusst nicht geplant werden (z. B. Servicetelefon, Verwaltung). Sie erzeugen keine Warnung; ihr Bedarf und ihre Dienste fallen weg. |
| `groups` | Liste `{match, areaIds}` | Ordnet SP5-Gruppen über ihren **Namen** (`5GROUP.NAME`) Bereichen zu. Eine Person darf in allen Bereichen ihrer Gruppen eingesetzt werden (Qualifikation). Mehrere Gruppen dürfen auf denselben Bereich zeigen. Pflicht. |
| `ignoreGroups` | Liste von Mustern | Gruppen ohne Bedeutung für die Qualifikation (Teams, "alle"). Sie erzeugen keine Warnung. |
| `excludeGroups` | Liste von Mustern | Wer in einer dieser Gruppen ist, wird **nicht** übernommen, auch wenn andere Gruppen Bereiche geben (Ehrenamt, Servicetelefon, Verwaltung). Ihr Bedarf fällt ebenfalls weg. |
| `compensatoryRestAbsences` | Liste von Mustern | Abwesenheitskürzel (`5LEAVT.SHORTNAME`), die Ersatzruhe sind. |
| `ignoreAbsences` | Liste von Mustern | Abwesenheitsarten, die nicht übernommen werden. |

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
  `entryDate`/`exitDate`; Dispofold verplant Tage außerhalb nicht und rechnet das Soll anteilig.
- **Schichten:** Id `S<SP5-Id>`, Kürzel, Name und Farbe aus SP5. Bei Zeiten je Wochentag gilt die
  häufigste Zeit. Schichten über 12 Stunden (die Standardregel `maxDutyHours` von Dispofold) werden nicht
  übernommen, weil Dispofold sonst den ganzen Import ablehnt. Ist `maxDutyHours` in Dispofold kleiner
  eingestellt, lehnt Dispofold längere Schichten trotzdem ab (Fehlercode `import.shiftTooLong`).
- **Einschränkungen** (`5RESTR`): `RESTRICT` 2 = nie, 1 = nur auf Anfrage, 0 = keine. Mit `--shifts-from-history`
  kommen "nur auf Anfrage" aus der Historie dazu.
- **Abwesenheiten** (`5LEAVT`, `5ABSEN`): je Person und Tag die erste, nur an Tagen der Beschäftigung.
  "Senkt das Soll" (`reducesTarget`), wenn SP5 die Art anrechnet (`CHARGETYP` nicht 0); "belastet das
  Stundenkonto" (`chargesAccount`), wenn sie zusätzlich von den Ist- oder Überstunden abgezogen wird
  (`DEDUCTACT` oder `DEDUCTOVT`, z. B. Zeitausgleich). Bitte für den echten Bestand in Dispofold prüfen.
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
| `Gruppe ohne Zuordnung: SP5-Id 7, Kürzel 'G7'` | Gruppe in `groups`, `ignoreGroups` oder `excludeGroups` aufnehmen. |
| `17 Mitarbeiter nicht übernommen (Grund)` | Gründe: ausgeblendet, nicht beschäftigt im Monat, ausgeschlossene Gruppe, ohne Bereich. Zahl mit dem erwarteten Personalstand vergleichen. |
| `Mitarbeiter E12 hat keine Wochenstunden (CALCBASE 1) und wird nicht übernommen` | Die Stunden der Grundlage in SP5 nachtragen (`HRSDAY` und `WORKDAYS`, `HRSWEEK`, `HRSMONTH` oder `HRSTOTAL`). |
| `Wochenstunden nach CALCBASE: 0 (Tag) 40, 1 (Woche) 12, ...; HRSWEEK weicht bei 25 davon ab` | Immer vorhanden. Viele Abweichungen unter `0` sind normal: SP5 rechnet dort nicht mit `HRSWEEK`. |
| `Mitarbeiter E12 hat Gesamtstunden (CALCBASE 3) ohne abgeschlossene Beschäftigung (EMPSTART bis EMPEND) und wird nicht übernommen` | In SP5 das Austrittsdatum eintragen oder eine andere Grundlage wählen. |
| `3 Mitarbeiter nur teilweise im Monat beschäftigt; ...` | Ein- oder Austritt im Monat. |
| `Bereich AN: 2 Mitarbeiter mit Diensten in den 12 Monaten davor ohne den Bereich aus Gruppen, 5 mit ...` | Immer vorhanden (außer ohne Historie); siehe [Bereiche aus der Historie](#bereiche-aus-der-historie). |
| `Bereiche aus Diensten (mindestens 3): ...` | Nur mit `--areas-from-history`: wie viele Bereiche ergänzt und wie viele Personen dadurch übernommen wurden. |
| `Schichten im Bereich AN: 4 Mitarbeiter mit keinem Dienst in mindestens einer Schicht des Bereichs ...` | Immer vorhanden (außer ohne Historie); siehe [Schichten aus der Historie](#schichten-aus-der-historie). |
| `Schichten aus Diensten: 3 Mitarbeiter mit zu kurzer Historie ... nicht bewertet, 20 Paare ... mit Einschränkung aus SP5 belassen` | Immer vorhanden (außer ohne Historie). |
| `Schichten aus Diensten (mindestens 3): 12 Einschränkungen 'nur auf Anfrage' ergänzt` | Nur mit `--shifts-from-history`. |
| `Bedarf im Bereich AN, Spitze je Tagestyp aus der Historie/aus SP5: MON 3/8, ...; 1200 Dienste ... gezählt, 17 nicht gezählt ...` | Immer vorhanden (außer ohne Historie); siehe [Bedarf aus der Historie](#bedarf-aus-der-historie). Eine große Abweichung ist bei SP5-Bedarf zu erwarten. Viele nicht gezählte Dienste: Zuordnung und Gruppen prüfen. |
| `Bedarf aus der Historie: Median ...` oder `Bedarf aus SP5 ...` | Immer vorhanden (außer ohne Historie): woher der Bedarf im Datensatz kommt. |
| `Stundenkonto und offene Ersatzruhetage werden aus SP5 nicht übernommen (0)` | Immer vorhanden; Werte in Dispofold pflegen. |
| `Tabelle 5XXX fehlt, wird als leer behandelt` | Falscher Ordner oder unvollständige Kopie. |
| `... Einschränkungen nur für Feiertage nicht übernommen` | Das Datenformat kennt keine Feiertagseinschränkung. |
| `... Einschränkungen ohne gültigen Grad (RESTRICT) als 'nie' übernommen` | Grad in SP5 prüfen. |
| `... weitere Abwesenheiten am selben Tag ignoriert` | Je Person und Tag zählt die erste Abwesenheit. |
| `Keine Feiertage in SP5 (5HOLID); ...` | Die österreichischen Feiertage werden verwendet. |
| `... halbe Feiertage (5HOLID INTERVAL) nicht als Feiertag übernommen` | Halbe Feiertage zählen nur für den SP5-Bedarf (`--demand sp5`) als Feiertag. |
| `Bereich X: am YYYY-MM-DD kein Bedarf in SP5, es gilt der Wochentagsbedarf` | Nur mit `--demand sp5`: SP5 hat an dem Tag keinen Bedarf; in Dispofold prüfen. |
| `... Dienste ... mit nicht zugeordneter Schicht ignoriert` | Vormonatsdienste in Schichten ohne Bereich. |
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
