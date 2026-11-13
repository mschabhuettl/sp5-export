# sp5-export

Kommandozeilenwerkzeug, das einen Planungsmonat aus einem Schichtplaner5-Datenordner (SP5) liest und als
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

- **Personen:** wer nicht ausgeblendet ist (`HIDE`), im Monat beschäftigt ist (`EMPSTART`/`EMPEND`), in
  keiner `excludeGroups`-Gruppe ist, Wochenstunden hat und über `groups` mindestens einen Bereich bekommt.
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
- **Einschränkungen** (`5RESTR`): `RESTRICT` 2 = nie, 1 = nur auf Anfrage, 0 = keine.
- **Abwesenheiten** (`5LEAVT`, `5ABSEN`): je Person und Tag die erste, nur an Tagen der Beschäftigung.
  "Senkt das Soll" (`reducesTarget`), wenn SP5 die Art anrechnet (`CHARGETYP` nicht 0); "belastet das
  Stundenkonto" (`chargesAccount`), wenn sie zusätzlich von den Ist- oder Überstunden abgezogen wird
  (`DEDUCTACT` oder `DEDUCTOVT`, z. B. Zeitausgleich). Bitte für den echten Bestand in Dispofold prüfen.
- **Bedarf** (`5SHDEM`, `5SPDEM`): je Bereich in Zeitfenster umgerechnet, Feiertage als `HOL`, Sonderbedarf
  und abweichende Tage als Regeln mit Datum.
- **Feiertage** aus `5HOLID` (nur ganztägige); fehlen sie, die österreichischen gesetzlichen Feiertage.
- **Vormonatsdienste** der letzten 7 Tage und **Historie** der drei Vormonate aus `5SPSHI`, `5MASHI`
  (Istplan) und den Dienstzyklen. Ein Sonderdienst (`5SPSHI`) mit Schicht ersetzt den Dienst des Tages, gleich
  welcher `TYPE` und auch dann, wenn seine Schicht nicht übernommen wird (wie in sp5lib; die Bedeutung von `TYPE` ist nicht dokumentiert, daher eine Warnung). An Tagen
  mit ganztägiger Abwesenheit (`5ABSEN`, `INTERVAL` 0) gilt kein Dienst; SP5 zeigt beides als Konflikt, der
  Datensatz kennt keine Abwesenheiten außerhalb des Planungsmonats. Historieneinträge (je Person und Monat mit Diensten oder Ersatzruhe) haben `source` `import`;
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
| `Stundenkonto und offene Ersatzruhetage werden aus SP5 nicht übernommen (0)` | Immer vorhanden; Werte in Dispofold pflegen. |
| `Tabelle 5XXX fehlt, wird als leer behandelt` | Falscher Ordner oder unvollständige Kopie. |
| `... Einschränkungen nur für Feiertage nicht übernommen` | Das Datenformat kennt keine Feiertagseinschränkung. |
| `... Einschränkungen ohne gültigen Grad (RESTRICT) als 'nie' übernommen` | Grad in SP5 prüfen. |
| `... weitere Abwesenheiten am selben Tag ignoriert` | Je Person und Tag zählt die erste Abwesenheit. |
| `Keine Feiertage in SP5 (5HOLID); ...` | Die österreichischen Feiertage werden verwendet. |
| `... halbe Feiertage (5HOLID INTERVAL) nicht als Feiertag übernommen` | Halbe Feiertage zählen nur für den Bedarf als Feiertag. |
| `Bereich X: am YYYY-MM-DD kein Bedarf in SP5, es gilt der Wochentagsbedarf` | SP5 hat an dem Tag keinen Bedarf; in Dispofold prüfen. |
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
