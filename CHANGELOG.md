# Änderungen

## Unveröffentlicht

- **Angewendete Korrekturen im Datensatz:** Der Datensatz hält im Abschnitt `appliedCorrections` fest, welche
  Einträge der Korrekturdatei angewendet wurden (Format der Korrekturdatei, Version 2, ohne `note`; ohne
  `--corrections` leer). Dispofold ab 0.15.0 exportiert damit eine vollständige Korrekturdatei, die
  `korrekturen.json` einfach ersetzt; das Zusammenführen von alter und neuer Datei von Hand entfällt
  ([docs/ablauf.md](docs/ablauf.md#3-bereinigen-korrekturen-in-dispofold)). Ältere Versionen von Dispofold
  ignorieren den Abschnitt.
- **Bericht:** neuer `status` `gleich_quelle` in den Zeilen `korrektur` für eine Korrektur, deren Wert derselbe wie
  der exportierte ist. Sie wird trotzdem angewendet und zählt als angewendet.
- **Datenformat:** Schema, `docs/datenformat.md` und Beispieldateien von Dispofold aktualisiert.
