# Datenformate

Die Werkzeuge tauschen zwei JSON-Dokumente aus:

- **Datensatz** (`dispofold/dataset`, Version 2): Stammdaten und Planungsmonate. Jede Datenquelle liefert
  genau dieses Dokument (ein Export aus dem bisherigen Planungssystem, die synthetischen Demodaten, später
  LOGA). Es wird unter **Import/Export**, **Datensatz importieren** hochgeladen.
- **Plan** (`dispofold/plan`): der Dienstplan eines Monats. Das ist das JSON-Exportformat; ein
  exportierter Plan lässt sich wieder importieren.

Allgemeine Regeln:

- Schlüssel sind englisch, Texte für Menschen deutsch.
- Datum `YYYY-MM-DD`, Monat `YYYY-MM`, Uhrzeit `HH:MM` (beim Lesen ist auch `HH:MM:SS` erlaubt),
  Zeitpunkt ISO 8601 UTC.
- Wochentage als ISO-Zahl 1 (Montag) bis 7 (Sonntag). Bei Bedarfsregeln als Kürzel `MON` ... `SUN` und
  `HOL` für Feiertage.
- Ids sind Zeichenketten. Personen-Ids sind interne Ids; sie sind das Einzige, was in Logs erscheint.
- Endet eine Schicht oder ein Bedarfsfenster nicht nach dem Beginn (`end <= start`), endet es am Folgetag.
- Unbekannte Schlüssel werden ignoriert. Ein JSON-Schema für Version 2 liegt in
  [`dataset.schema.json`](dataset.schema.json); Beispieldateien, die angenommen oder abgelehnt werden,
  liegen in `tests/fixtures/contract/accept` und `tests/fixtures/contract/reject`.

## Datensatz

```json
{
  "format": "dispofold/dataset",
  "version": 2,
  "source": "synthetic",
  "areas": [
    { "id": "AN", "name": "Notrufannahme", "color": "#0F766E" },
    { "id": "TV", "name": "Transportvermittlung", "color": "#2563EB" }
  ],
  "shifts": [
    { "id": "AN-F", "areaId": "AN", "code": "A1", "name": "Annahme früh 06:30",
      "start": "06:30", "end": "14:30", "category": "EARLY", "color": "#F6E3D3", "unpopular": false },
    { "id": "AN-N", "areaId": "AN", "code": "A3", "name": "Annahme Nacht 19:00",
      "start": "19:00", "end": "07:00", "category": "NIGHT", "color": "#D9D3F6", "unpopular": true },
    { "id": "TV-T", "areaId": "TV", "code": "T2", "name": "Vermittlung Tag 07:00",
      "start": "07:00", "end": "19:00", "category": "DAY", "color": "#D3E6F6", "unpopular": false }
  ],
  "demand": [
    { "areaId": "AN", "days": ["MON","TUE","WED","THU","FRI","SAT","SUN"], "date": null,
      "from": "06:30", "to": "07:00", "min": 1, "max": null },
    { "areaId": "TV", "days": null, "date": "2026-12-24",
      "from": "07:00", "to": "19:00", "min": 1, "max": 2 }
  ],
  "holidays": [ { "date": "2026-12-25", "name": "Christtag" } ],
  "absenceTypes": [
    { "code": "U", "name": "Urlaub", "reducesTarget": true, "chargesAccount": false,
      "compensatoryRest": false, "color": "#A3C585" },
    { "code": "ZA", "name": "Zeitausgleich", "reducesTarget": true, "chargesAccount": true,
      "compensatoryRest": false, "color": "#C9B8E8" },
    { "code": "K", "name": "Krankenstand", "reducesTarget": true, "chargesAccount": false,
      "compensatoryRest": false, "color": "#E8B8B8" },
    { "code": "ER", "name": "Ersatzruhe", "reducesTarget": true, "chargesAccount": false,
      "compensatoryRest": true, "color": "#8FD3C7" }
  ],
  "people": [
    { "id": "P0001", "firstName": "Anna", "lastName": "Beispiel", "shortName": "ABE",
      "weeklyHours": 40.0, "areaIds": ["AN", "TV"], "hoursAccount": 6.5, "compRestBalance": 2,
      "familyRules": [ { "weekdays": [2, 4], "latestEnd": "18:00", "earliestStart": null,
                         "noNights": true, "note": "Kinderbetreuung" } ],
      "externalId": "17", "entryDate": null, "exitDate": null },
    { "id": "P0002", "firstName": "Bernd", "lastName": "Muster", "shortName": "BMU",
      "weeklyHours": 40.0, "areaIds": ["AN"], "hoursAccount": 0.0, "compRestBalance": 0,
      "familyRules": [], "externalId": "18", "entryDate": "2026-12-14", "exitDate": null }
  ],
  "restrictions": [
    { "personId": "P0001", "shiftId": "AN-F", "category": null, "weekday": 0, "kind": "NEVER",
      "validFrom": null, "validTo": null },
    { "personId": "P0002", "shiftId": null, "category": "NIGHT", "weekday": 0, "kind": "ON_REQUEST",
      "validFrom": "2026-12-14", "validTo": "2027-03-31" }
  ],
  "history": [
    { "personId": "P0001", "month": "2026-10", "source": "import", "nights": 3, "weekendsWorked": 2,
      "holidays": 0, "unpopular": 2, "wishes": 4, "wishesMet": 3, "hours": 171.5,
      "hoursAccountAfter": 6.5, "compRestBalanceAfter": 2, "compRestUsed": 1, "compRestEarned": 0 }
  ],
  "periods": [
    { "month": "2026-12",
      "absences": [ { "personId": "P0001", "date": "2026-12-07", "typeCode": "U" } ],
      "wishes": [ { "personId": "P0001", "date": "2026-12-24", "kind": "OFF", "shiftId": null } ],
      "fixed": [ { "personId": "P0001", "date": "2026-12-01", "shiftId": "TV-T" } ],
      "previousDuties": [ { "personId": "P0001", "date": "2026-11-30", "shiftId": "AN-N" } ] }
  ]
}
```

Alle Listen auf oberster Ebene und die vier Listen eines Planungsmonats müssen vorhanden sein, dürfen aber
leer sein. Felder, die im Beispiel `null` sind, dürfen fehlen.

### Felder

| Objekt | Feld | Bedeutung |
|---|---|---|
| area | `id`, `name`, `color` | Planungsbereich, z. B. Notrufannahme, Disposition Rettung, Transportvermittlung, Lageführung |
| shift | `areaId` | Bereich, zu dem die Schicht gehört |
| shift | `code` | Kurzzeichen im Raster |
| shift | `start`, `end` | Dienstbeginn und -ende; die Dauer ist die bezahlte Dienstzeit (bezahlte Pausen sind enthalten). Höchstens so lang wie die Regel „Höchstdauer eines Dienstes" (`maxDutyHours`, Standard 12 Stunden) |
| shift | `category` | `EARLY`, `DAY`, `LATE`, `NIGHT`; fehlt sie, wird sie aus den Zeiten abgeleitet |
| shift | `unpopular` | zählt in der Fairnessübersicht als unbeliebter Dienst |
| demand | `days` / `date` | Gültigkeit: Tagestypen `MON`..`SUN`, `HOL`, oder ein einzelnes Datum |
| demand | `from`, `to`, `min`, `max` | Zeitfenster und Mindest- (optional Höchst-) Besetzung des Bereichs |
| absenceType | `code` | Kürzel, auf das sich Abwesenheiten beziehen |
| absenceType | `reducesTarget` | „senkt das Soll": an Werktagen (Montag bis Freitag, kein Feiertag) sinkt das Soll um Wochenstunden / 5 |
| absenceType | `chargesAccount` | „belastet das Stundenkonto": die Stunden, um die das Soll sinkt, gehen vom Stundenkonto ab |
| absenceType | `compensatoryRest` | die Art ist Ersatzruhe und senkt die offenen Ersatzruhetage |
| person | `weeklyHours` | Wochenstunden laut Vertrag, größer als 0 |
| person | `areaIds` | Bereiche, in denen die Person eingesetzt werden darf |
| person | `hoursAccount` | Stundenkonto aus Vormonaten (positiv: Plusstunden) |
| person | `compRestBalance` | offene Ersatzruhetage |
| person | `familyRules` | Familienwunsch je Wochentag (1..7): spätestes Ende, frühester Beginn, keine Nacht |
| person | `externalId` | Id im Quellsystem, z. B. Personalnummer |
| person | `entryDate`, `exitDate` | erster und letzter Tag der Beschäftigung, `null` = offen. Tage außerhalb werden nicht verplant, das Soll wird anteilig berechnet |
| restriction | `shiftId` / `category` | genau eines von beiden: eine Schicht oder alle Schichten einer Kategorie (z. B. `NIGHT`) |
| restriction | `weekday` | 1..7, 0 = jeden Tag |
| restriction | `kind` | `NEVER` (nie, hart) oder `ON_REQUEST` (nur auf Anfrage, weich) |
| restriction | `validFrom`, `validTo` | erster und letzter Tag, an dem die Einschränkung gilt, `null` = offen |
| history | | Kennzahlen früherer Monate für die Fairness über mehrere Monate |
| history | `source` | `import` (Istwerte aus dem Quellsystem) oder `plan` (beim Monatsabschluss geschrieben) |
| history | `hours` | geleistete Dienststunden im Monat |
| history | `hoursAccountAfter` | Stundenkonto nach dem Monat, `null` = unbekannt |
| history | `compRestBalanceAfter` | offene Ersatzruhetage nach dem Monat, `null` = unbekannt |
| history | `compRestUsed`, `compRestEarned` | im Monat genommene und (durch Feiertagsarbeit) erworbene Ersatzruhetage |
| period | `absences` | Abwesenheiten mit Abwesenheitsart |
| period | `wishes` | `OFF` (frei), `DUTY` (Dienst), `SHIFT` (diese Schicht), `NOT_SHIFT` (diese Schicht nicht); bei `SHIFT` und `NOT_SHIFT` ist `shiftId` Pflicht |
| period | `fixed` | fixe Dienste; `shiftId: null` fixiert einen freien Tag |
| period | `previousDuties` | Dienste der letzten Tage des Vormonats (Ruhezeit und Blöcke am Monatsanfang) |

Beispiele für Abwesenheitsarten:

| Art | `reducesTarget` | `chargesAccount` |
|---|---|---|
| Urlaub | `true` | `false` |
| Zeitausgleich (ZA) | `true` | `true` |
| Krankenstand | `true` | `false` |

Importierte Historieneinträge (`source: "import"`) sind Istwerte. Ein Monatsabschluss überschreibt sie
nicht; er schreibt eigene Einträge mit `source: "plan"`.

### Bedarf

Der Bedarf gilt je Bereich und Tageszeit, nicht je Schicht. Der Solver wählt unter den Schichten des
Bereichs. An einem Feiertag ersetzen die `HOL`-Regeln eines Bereichs dessen Wochentagsregeln (falls der
Bereich `HOL`-Regeln hat). Regeln mit `date` ersetzen für diesen Bereich an diesem Tag alle anderen.

### Prüfung beim Import

Version 1 wird nicht mehr angenommen. Ein Datensatz wird nur als Ganzes übernommen: Findet die Prüfung
einen Fehler, wird nichts gespeichert und alle Fehler werden mit Code und Parametern gemeldet (siehe
[Fehlercodes beim Import](#fehlercodes-beim-import)). Einträge werden nie stillschweigend verworfen.
Warnungen betreffen harmlose Fälle; der Import läuft trotzdem.

Abgelehnt wird insbesondere:

- falsches `format` oder eine andere `version` als 2;
- ein Datensatz ohne Personen oder ohne Schichten;
- fehlende Pflichtwerte: Ids, Beginn und Ende einer Schicht, `from` und `to` einer Bedarfsregel, Datum
  von Feiertagen, Abwesenheiten, Wünschen, fixen Diensten und Vormonatsdiensten, die Listen des Datensatzes
  und eines Planungsmonats;
- doppelte Ids bei Bereichen, Schichten, Personen und Abwesenheitsarten, doppelte Planungsmonate, zwei
  fixe Dienste derselben Person am selben Tag, zwei Historieneinträge derselben Person im selben Monat;
- Verweise auf unbekannte Bereiche, Personen, Schichten oder Abwesenheitsarten;
- Monate in der Historie, die nicht `YYYY-MM` sind, und eine `source` außer `import` und `plan`;
- Wochenstunden kleiner oder gleich 0, `exitDate` vor `entryDate`;
- unbekannte Tagestypen im Bedarf, eine Bedarfsregel ohne `days` und ohne `date`, `min` kleiner als 0 oder
  `max` kleiner als `min`;
- Wochentage einer Einschränkung außerhalb von 0..7 oder eines Familienwunsches außerhalb von 1..7, eine
  Einschränkung ohne oder mit beiden von `shiftId` und `category`, `validTo` vor `validFrom`;
- ein Wunsch `SHIFT` oder `NOT_SHIFT` ohne `shiftId`;
- Abwesenheiten, Wünsche und fixe Dienste außerhalb ihres Planungsmonats, Vormonatsdienste nicht vor
  dessen Beginn;
- Schichten, die länger als die Regel „Höchstdauer eines Dienstes" (`maxDutyHours`) sind (`end <= start` heißt Folgetag, gleiche
  Zeiten also 24 Stunden);
- die Schicht-Id `ER`; sie ist für geplante Ersatzruhe reserviert.

Dateien, die schon nicht zum Format passen (kein JSON, ein Monat eines Planungsmonats oder ein Datum, das
kein gültiges Datum ist, unbekannte Aufzählungswerte wie `kind`), werden vor dieser Prüfung abgelehnt.

Ein einzelner Planungsmonat, der in der Oberfläche erfasst wird, wird mit denselben Regeln gegen die
gespeicherten Stammdaten geprüft.

## Plan

```json
{
  "format": "dispofold/plan",
  "version": 1,
  "month": "2026-12",
  "status": "SOLVED",
  "score": "0hard/0medium/-123456soft",
  "seed": 42,
  "updated": "2026-11-12T10:00:00Z",
  "assignments": [
    { "personId": "P0001", "date": "2026-12-01", "shiftId": "AN-T", "compRest": false, "pinned": true },
    { "personId": "P0001", "date": "2026-12-02", "shiftId": null, "compRest": true, "pinned": false }
  ]
}
```

- Eine Zeile je Person und Tag. `shiftId: null` ist ein freier Tag, `compRest: true` ein geplanter
  Ersatzruhetag.
- `pinned: true` heißt: von der Planung festgelegt, der Solver ändert es beim Neuplanen nicht.
- `status`: `EMPTY`, `SOLVING`, `SOLVED`, `STOPPED`, `FAILED`, `CLOSED` (Monat abgeschlossen).
- Der Score hat drei Stufen: `hard` (Regeln aus KV und Qualifikation, muss 0 sein), `medium` (nicht
  gedeckter Bedarf in Personenminuten) und `soft` (Wünsche, Fairness, Blöcke usw.).

## Fehlercodes beim Import

Jeder Befund hat einen Code und Parameter. Die Parameter enthalten nur Ids, Zahlen und Daten
(`YYYY-MM-DD`, Monat `YYYY-MM`), nie Namen. Parameter ohne Wert fehlen. Die Oberfläche zeigt dazu einen
deutschen Text an.

`kind` nennt die Art des Eintrags: `dataset`, `area`, `shift`, `demand`, `holiday`, `absenceType`,
`person`, `familyRule`, `restriction`, `history`, `period`, `absence`, `wish`, `fixed`, `previousDuty`.
`index` ist die Position des Eintrags in seiner Liste (ab 0), `month` der Planungsmonat oder bei `history`
der Monat des Eintrags.

### Fehler

| Code | Parameter | Bedeutung |
|---|---|---|
| `import.format` | | kein Datensatz (`format` ist nicht `dispofold/dataset`) |
| `import.version` | `version` | Version wird nicht unterstützt (nur 2) |
| `import.noPeople` | | keine Personen |
| `import.noShifts` | | keine Schichten |
| `import.missingField` | `kind`, `field`, `index`, `month` | Pflichtwert oder Pflichtliste fehlt (`field` fehlt, wenn der ganze Eintrag `null` ist) |
| `import.duplicateId` | `kind`, `id` | Id doppelt (`area`, `shift`, `person`, `absenceType`; bei `period` ist `id` der Monat) |
| `import.duplicateEntry` | `kind`, `personId`, `date` oder `month` | zwei fixe Dienste derselben Person am selben Tag (`fixed`) oder zwei Historieneinträge derselben Person im selben Monat (`history`) |
| `import.unknownArea` | `kind`, `areaId`, `shiftId` / `personId` / `index` | Schicht, Person oder Bedarfsregel verweist auf einen unbekannten Bereich |
| `import.unknownPerson` | `kind`, `personId`, `date`, `month` | Einschränkung, Historie, Abwesenheit, Wunsch, fixer Dienst oder Vormonatsdienst verweist auf eine unbekannte Person |
| `import.unknownShift` | `kind`, `personId`, `shiftId`, `date` | Einschränkung, Wunsch, fixer Dienst oder Vormonatsdienst verweist auf eine unbekannte Schicht |
| `import.unknownAbsenceType` | `personId`, `typeCode`, `date` | unbekannte Abwesenheitsart |
| `import.badMonth` | `personId`, `month` | Monat eines Historieneintrags ist nicht `YYYY-MM` |
| `import.badHistorySource` | `personId`, `month` | `source` eines Historieneintrags ist weder `import` noch `plan` |
| `import.badWeeklyHours` | `personId`, `weeklyHours` | Wochenstunden kleiner oder gleich 0 |
| `import.badEmployment` | `personId` | `exitDate` liegt vor `entryDate` |
| `import.badDemandDay` | `areaId`, `index`, `day` | unbekannter Tagestyp im Bedarf (erlaubt `MON`..`SUN`, `HOL`) |
| `import.badDemand` | `areaId`, `index` | Bedarfsregel ohne `days` und ohne `date`, `min` kleiner als 0 oder `max` kleiner als `min` |
| `import.badWeekday` | `kind`, `personId`, `weekday` | Wochentag einer Einschränkung nicht in 0..7 oder eines Familienwunsches nicht in 1..7 |
| `import.restrictionTarget` | `personId` | Einschränkung hat nicht genau eines von `shiftId` und `category` |
| `import.badValidity` | `personId` | `validTo` einer Einschränkung liegt vor `validFrom` |
| `import.wishWithoutShift` | `personId`, `date` | Wunsch `SHIFT` oder `NOT_SHIFT` ohne `shiftId` |
| `import.dateOutsideMonth` | `kind`, `personId`, `date`, `month` | Abwesenheit, Wunsch oder fixer Dienst außerhalb des Planungsmonats, Vormonatsdienst nicht vor dessen Beginn |
| `import.missingTime` | `shiftId` | Beginn oder Ende einer Schicht fehlt |
| `import.shiftTooLong` | `shiftId`, `minutes`, `maxMinutes` | Schicht ist länger als die Höchstdauer eines Dienstes (`maxDutyHours`) |
| `import.shiftIdReserved` | `shiftId` | Schicht-Id `ER` ist für Ersatzruhe reserviert |

### Warnungen

| Code | Parameter | Bedeutung |
|---|---|---|
| `import.duplicateAbsence` | `personId`, `date` | mehrere Abwesenheiten derselben Person am selben Tag; die erste gilt |
| `import.historyInFuture` | `personId`, `month` | Historieneintrag für einen geplanten Monat oder später |
| `import.duplicateRestriction` | `personId`, `shiftId` oder `category`, `weekday` | mehrere Einschränkungen derselben Person für dieselbe Schicht oder Kategorie am selben Wochentag |
