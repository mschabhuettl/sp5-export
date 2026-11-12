"""
Reiner Python-Reader für die DBF/dBASE-Dateien von Schichtplaner5.
Behandelt die UTF-16-LE-Textkodierung der Delphi/FoxPro-Anwendung.
"""

import io
import struct
from datetime import date
from typing import Any

#: Binäre C-Felder (Spec D-21): der Inhalt sind rohe Bytes, kein Text. Sie
#: werden als ungestrippte ``bytes`` in voller Feldbreite gelesen (und
#: geschrieben). Der Namensabgleich ist für das feste 30-Tabellen-Schema
#: exakt — gegen die Referenz-DB-Header geprüft: DIGEST gibt es nur in 5USER,
#: CREATIME und UUID nur in 5BUILD. (RESERVED-Felder bleiben bewusst außen
#: vor: bestehende Aufrufer konsumieren sie als Strings.)
BINARY_C_FIELDS = {"DIGEST", "CREATIME", "UUID"}


def _dedupe_names(names: list[str]) -> list[str]:
    """Doppelte Feldnamen positionsbasiert eindeutig machen.

    Der physische 5DADEM-Header führt *zwei* Felder namens ``START``
    (Original-Schemafehler, Spec D-55; das Original bindet Felder ordinal,
    D-12, dort ist das Duplikat harmlos). Da Datensätze als dicts exponiert
    werden, bekommt das zweite (und jedes weitere) Vorkommen eines Namens ein
    Zahlensuffix: das Zeitraum-Ende von 5DADEM heißt ``START2``. Der
    Schreibpfad (``dbf_writer``) nutzt dieselbe Konvention, sodass
    ``START``/``START2`` round-trippen. Keine SP5-Tabelle enthält einen
    Feldnamen, der mit einem generierten Suffixnamen kollidiert.
    """
    seen: dict[str, int] = {}
    result = []
    for name in names:
        seen[name] = seen.get(name, 0) + 1
        result.append(name if seen[name] == 1 else f"{name}{seen[name]}")
    return result


def _is_utf16_le(raw: bytes) -> bool:
    """
    Heuristik: erkennt, ob rohe Bytes UTF-16-LE-kodierter Text sind.

    In UTF-16-LE-Latin-1-Text sind die Bytes an ungeraden Positionen
    (1, 3, 5, …) 0x00; für nicht-lateinische Schriften bis Arabisch
    (Griechisch 0x03xx, Kyrillisch 0x04xx, Hebräisch 0x05xx, Arabisch 0x06xx)
    sind sie 0x01..0x07. Reine ASCII-Datenfelder (WORKDAYS, STARTEND*, …)
    enthalten an ungeraden Positionen nur druckbare Bytes >= 0x20 — jedes
    ungerade Byte < 0x08 markiert also UTF-16-LE-Text. Bekannte Grenze: Text,
    der NUR aus Zeichen >= U+0800 besteht (z. B. reines CJK), wird weiterhin
    fälschlich als ASCII erkannt.
    """
    if len(raw) < 4:
        # Sehr kurzes Feld — prüfen, ob das zweite Byte ein UTF-16-High-Byte ist
        return len(raw) >= 2 and raw[1] < 0x08
    # Für die Erkennung bis zu 8 Bytes betrachten
    sample_len = min(8, len(raw))
    sample = raw[:sample_len]
    odd_bytes = sample[1::2]
    high_count = sum(1 for b in odd_bytes if b < 0x08)
    # Mehr als die Hälfte der ungeraden Bytes sind High-Bytes → UTF-16 LE
    return high_count > len(odd_bytes) // 2


def _decode_string(raw: bytes) -> str:
    """
    Dekodiert ein Zeichenfeld aus Schichtplaner5-.DBF-Dateien.

    SP5 nutzt zwei Kodierungen für Character-Felder:
    - Textfelder (NAME, SHORTNAME, …): UTF-16 LE, mit 0x20 aufgefüllt
    - Datenfelder (WORKDAYS, STARTEND*, …): reines ASCII, mit 0x20 aufgefüllt

    UTF-16 LE wird über die 0x00-Bytes an ungeraden Positionen erkannt.
    """
    if not raw:
        return ""

    if _is_utf16_le(raw):
        # UTF-16-LE-Text: Null-Terminator suchen (0x00 0x00 an gerader Position)
        end = len(raw)
        for i in range(0, len(raw) - 1, 2):
            if raw[i] == 0x00 and raw[i + 1] == 0x00:
                end = i
                break
        chunk = raw[:end]
        if not chunk:
            return ""
        try:
            return chunk.decode("utf-16-le").strip()
        except Exception:
            pass

    # Reines ASCII-/Binär-Datenfeld (WORKDAYS, STARTEND*, …): abschließende
    # Leerzeichen/Nullen strippen und als latin-1 dekodieren (erhält alle Bytewerte)
    stripped = raw.rstrip(b"\x00\x20")
    try:
        return stripped.decode("latin-1").strip()
    except Exception:
        return raw.split(b"\x00")[0].decode("latin-1", errors="replace").strip()


def _parse_date(raw: str) -> str | None:
    """Parst einen dBASE-Datumsstring YYYYMMDD ins ISO-Format.

    Liefert None für alles, was kein reales Kalenderdatum ist. Die volle
    Kalender-Validierung (über :class:`datetime.date`) weist unmögliche Daten
    wie ``20230231`` (31. Februar) ab, die der frühere ``day <= 31``-Check
    durchließ und die später ``date.fromisoformat`` hätten crashen lassen.
    """
    s = raw.strip()
    if len(s) == 8 and s.isdigit():
        try:
            year, month, day = int(s[:4]), int(s[4:6]), int(s[6:8])
            if year > 0:
                # date() validiert Monat/Tag für das jeweilige Jahr beim Konstruieren.
                return date(year, month, day).isoformat()
        except ValueError:
            pass
    return None


# Vorberechnete Feld-Spezifikation: (dict-Schlüssel, Typ, Länge, Nachkomma,
# Binärfeld?, Startoffset). Wird je Tabelle EINMAL erstellt und für alle
# Datensätze wiederverwendet — spart die früher pro Datensatz wiederholten
# int()/str()/Set-Lookups (messbarer Gewinn beim Parsen großer Tabellen).
def _compile_field_specs(
    fields: list[dict], names: list[str]
) -> list[tuple[str, str, int, int, bool, int]]:
    specs: list[tuple[str, str, int, int, bool, int]] = []
    offset = 1  # Lösch-Flag überspringen
    for field, fname in zip(fields, names, strict=True):
        flen = int(field["len"])
        specs.append(
            (
                fname,
                str(field["type"]),
                flen,
                int(field["dec"]),
                str(field["name"]) in BINARY_C_FIELDS,
                offset,
            )
        )
        offset += flen
    return specs


def _parse_record_specs(
    raw: bytes, specs: list[tuple[str, str, int, int, bool, int]]
) -> dict[str, Any]:
    """Parst einen rohen Datensatz anhand vorberechneter Feld-Spezifikationen.

    Identische Dekodierlogik wie früher in ``_parse_record`` — nur ohne die
    je Datensatz wiederholte Ableitung der Felddaten. Binäre C-Felder
    (``BINARY_C_FIELDS``) kommen als rohe, ungestrippte ``bytes`` zurück.
    """
    record: dict[str, Any] = {}
    for fname, ftype, flen, fdec, is_binary, offset in specs:
        chunk = raw[offset : offset + flen]

        val: Any = None
        if ftype == "C":
            if is_binary:
                # Binärfeld (D-21): rohe Bytes, ungestrippt (ein gestrippter oder
                # latin-1-dekodierter MD5-Digest ist unwiederbringlich verfälscht).
                val = chunk
            else:
                # Zeichenfeld — UTF-16 LE in Schichtplaner5
                val = _decode_string(chunk)
        elif ftype == "D":
            # Datumsfeld YYYYMMDD
            val = _parse_date(chunk.decode("ascii", errors="replace"))
        elif ftype in ("N", "F"):
            # Numerisch/Float
            s = chunk.decode("ascii", errors="replace").strip()
            if s == "" or s == ".":
                val = 0
            else:
                try:
                    val = float(s) if "." in s or fdec > 0 else int(s)
                except ValueError:
                    val = 0
        elif ftype == "L":
            # Logisch
            s = chunk.decode("ascii", errors="replace").strip()
            val = s in ("T", "t", "Y", "y", "1")
        elif ftype == "M":
            # Memo (nur Zeiger in der .DBF, eigentliche Daten in der .DBT)
            val = None
        else:
            val = chunk.decode("ascii", errors="replace").strip()

        record[fname] = val

    return record


def _parse_record(
    raw: bytes, fields: list[dict], names: list[str] | None = None
) -> dict[str, Any]:
    """Parst einen rohen Datensatz-Bytestring in ein dict.

    *names* sind die (deduplizierten) dict-Schlüssel der Felder; werden aus den
    Felddeskriptoren berechnet, wenn nicht übergeben. Binäre C-Felder
    (``BINARY_C_FIELDS``) kommen als rohe, ungestrippte ``bytes`` zurück.

    Dünner Wrapper um :func:`_parse_record_specs` für Einzeldatensatz-Aufrufer
    (cdx_writer/dbf_writer); ``read_dbf`` berechnet die Specs einmalig selbst.
    """
    if names is None:
        names = _dedupe_names([str(f["name"]) for f in fields])
    return _parse_record_specs(raw, _compile_field_specs(fields, names))


def read_dbf(filepath: str, encoding_hint: str = "utf-16-le") -> list[dict[str, Any]]:
    """
    Liest eine .DBF-Datei und liefert die Datensätze als Liste von dicts.
    Zeichenfelder werden als UTF-16 LE dekodiert (wie von Schichtplaner5
    genutzt); binäre C-Felder (``BINARY_C_FIELDS``) kommen als rohe ``bytes``.
    Doppelte Feldnamen werden positionsbasiert eindeutig gemacht
    (``_dedupe_names``): das zweite ``START``-Feld von 5DADEM heißt ``START2``.

    Liefert eine leere Liste, wenn die Datei fehlt, nicht lesbar oder
    beschädigt ist — Aufrufer behandeln ein leeres Ergebnis als „keine Daten"
    und dürfen nicht crashen.
    """
    try:
        with open(filepath, "rb") as f:
            data = f.read()
    except OSError:
        # Datei fehlt, keine Rechte, oder zwischen Existenz-Check und open gelöscht
        return []
    return read_dbf_buffer(data)


def read_dbf_buffer(data: bytes) -> list[dict[str, Any]]:
    """Parst einen bereits eingelesenen .DBF-Bytepuffer (siehe :func:`read_dbf`).

    Getrennt vom Dateizugriff, damit Aufrufer die Bytes nur EINMAL lesen müssen
    (z. B. der DBF-Cache, der dieselben Bytes auch für die Inhalts-Prüfung hasht)
    — wichtig bei langsamen Bind-Mounts/Netzpfaden.
    """
    if len(data) < 32:
        return []
    f = io.BytesIO(data)
    # Header lesen (32 Bytes)
    header = f.read(32)

    num_records = struct.unpack_from("<I", header, 4)[0]
    header_size = struct.unpack_from("<H", header, 8)[0]
    record_size = struct.unpack_from("<H", header, 10)[0]

    # Felddeskriptoren lesen (je 32 Bytes, terminiert mit 0x0D)
    fields: list[dict[str, Any]] = []
    f.seek(32)
    while True:
        field_data = f.read(32)
        if not field_data or len(field_data) < 32 or field_data[0] == 0x0D:
            break
        name = (
            field_data[0:11]
            .split(b"\x00")[0]
            .decode("ascii", errors="replace")
            .strip()
        )
        ftype = chr(field_data[11])
        flen = field_data[16]
        fdec = field_data[17]
        fields.append({"name": name, "type": ftype, "len": flen, "dec": fdec})

    # Datensätze lesen
    f.seek(header_size)
    records = []
    names = _dedupe_names([str(f_["name"]) for f_ in fields])
    # Feld-Specs einmal je Tabelle berechnen und für alle Datensätze nutzen.
    specs = _compile_field_specs(fields, names)

    for _ in range(num_records):
        raw = f.read(record_size)
        if not raw or len(raw) < record_size:
            break

        # Gelöschte Datensätze überspringen (erstes Byte = 0x2A = '*')
        if raw[0] == 0x2A:
            continue

        records.append(_parse_record_specs(raw, specs))

    return records


def get_table_fields(filepath: str) -> list[dict[str, Any]]:
    """Liefert die Felddefinitionen einer .DBF-Datei."""
    try:
        open_file = open(filepath, "rb")
    except OSError:
        return []
    with open_file as f:
        hdr = f.read(32)
        if len(hdr) < 32:
            return []  # leere oder abgeschnittene Datei
        fields = []
        while True:
            field_data = f.read(32)
            if not field_data or len(field_data) < 32 or field_data[0] == 0x0D:
                break
            name = (
                field_data[0:11]
                .split(b"\x00")[0]
                .decode("ascii", errors="replace")
                .strip()
            )
            ftype = chr(field_data[11])
            flen = field_data[16]
            fdec = field_data[17]
            fields.append({"name": name, "type": ftype, "len": flen, "dec": fdec})
    return fields
