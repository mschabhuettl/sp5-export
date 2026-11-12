"""
DBF-Schreibsupport für Schichtplaner5-Datenbanken.

Implementiert:
  append_record(filepath, fields, record)   – neuen Datensatz an eine .DBF anhängen
  update_record(filepath, fields, idx, d)   – Felder eines Datensatzes in-place überschreiben
  delete_record(filepath, fields, index)    – Datensatz als gelöscht markieren (0x2A-Flag)
  find_all_records(filepath, fields, **kw) – alle Datensätze zu Filterkriterien finden

Kodierungs-Kontrakt (byteweise gegen die Original-Beispiel-DB verifiziert):
  • Text-(C)-Felder (Spec D-19): UTF-16-LE-Stringbytes + \x00\x00-Null-
    Terminator + \x20-Auffüllung bis field_len.
    Leere Strings: \x00\x00 + \x20 * (field_len - 2).
  • ASCII-Klasse-(C)-Felder (Spec D-20/D-31: WORKDAYS, VALIDDAYS, DAILYDEM,
    STARTEND*, CATEGORY/REPORT in 5USER): reine cp1252-Bytes, \x20-aufgefüllt.
    Leerer Wert: nur Leerzeichen.
  • Binär-(C)-Felder (Spec D-21: DIGEST, CREATIME, UUID): rohe Bytes,
    \x00-aufgefüllt.
  • Datums-(D)-Felder: 'YYYYMMDD' ASCII, mit Leerzeichen auf field_len aufgefüllt.
  • Numerische (N)-Felder: rechtsbündiger ASCII-Dezimalstring, links mit
    Leerzeichen aufgefüllt.
  • Float-(F)-Felder (Spec D-15): rechtsbündige ASCII-Dezimalzahl mit exakt 4
    Nachkommastellen (Orakel: '7.7000'), unabhängig vom dec-Byte des
    Deskriptors (das SP5-Schema deklariert jedes F-Feld als 'F 19 dec=0').
  • Logische (L)-Felder: 'T' oder 'F' (1 Byte; von SP5 ungenutzt, Spec D-18).
  • Memo-(M)-Felder: nur Leerzeichen (von diesem Modul nicht geschrieben).
  • Doppelte Feldnamen (die zwei START-Felder von 5DADEM, Spec D-55) werden
    positionsbasiert aufgelöst: das zweite Vorkommen heißt in Datensatz-dicts
    'START2', passend zum Lesepfad (dbf_reader._dedupe_names).

Änderungsjournal (-L-Begleittabellen, Spec §2.7):
  • Jedes append/update/delete hängt zusätzlich einen Eintrag an '<table>-L.DBF'
    an: NUMBER = letzte Journal-NUMBER + 1, CHANGEID1..3 = zusammengesetzter
    Schlüssel des Datensatzes nach Spec D-41 (ungenutzte Komponenten 0),
    CHANGE = 1 (Satz angelegt/geändert, Upsert-Semantik) oder 2 (Satz
    gelöscht). Original-Clients pollen diese Journale, um externe Änderungen
    mitzubekommen (Spec D-69/D-71/D-76).
  • Journaling ist bedingungslos — die -L-Pflege des Originals hängt NICHT an
    5USETT.CHANGELOG (Spec D-68).
  • Fehlt die -L-Datei, wird der Journaleintrag mit Warnung übersprungen; der
    Haupt-Write gelingt. (Das Original legt fehlende Begleitdateien beim
    nächsten Öffnen neu an, Spec D-14.)

CDX-Indexdateien (Spec D-13/D-14):
  • SP5/CodeBase erzeugt beim Öffnen nur *fehlende* Indexdateien neu; eine
    vorhandene veraltete CDX würde weiterverwendet und passte nach einem Write
    aus diesem Modul nicht mehr zur Tabelle. Standardmäßig werden die
    .CDX-Dateien der geänderten Tabelle (Haupt- und -L-Datei) nach jedem
    erfolgreichen Write GELÖSCHT, sodass das Original sie aus seinen
    einkompilierten Tag-Definitionen neu aufbaut (D-14). Header-Byte 28
    (MDX/CDX-Flag, 0x01 in allen Originaldateien) bleibt unangetastet.
    INVALIDATE_CDX = False schaltet das ab (nur sicher, wenn die Daten nie
    wieder von einem Original-SP5-Client geöffnet werden).
  • OPTIONALER In-place-CDX-Writer (ROADMAP §B.2): WRITE_CDX = True (oder env
    SP5_CDX_WRITE=1) baut die .CDX nach jedem Write stattdessen über
    sp5lib.cdx_writer in place neu, sodass das Original die Tabelle ohne
    Index-Neuaufbau öffnet. Standardmäßig AUS; der bewährte Lösch-und-neu-
    aufbauen-Pfad bleibt der Default. Der Writer reproduziert die CDX der
    Beispiel-DB byteweise für das Ein-Tag-SP5-Schema (numerische und
    zusammengesetzte Zeichen-Schlüssel, mehrseitige B-Bäume) und fällt für
    jede nicht abbildbare Schlüsselform aufs Löschen zurück — Aktivieren ist
    also nie schlechter als der Default. database.py braucht keinen Hook:
    Schalter und Dispatch leben vollständig in diesem Modul.

Schreibsicherheit:
  • Exklusives fcntl.flock() um alle Schreiboperationen.
  • Header-Bytes 1–3 (JJ MM TT der letzten Änderung) bei jedem Write aktualisiert.
  • EOF-Marker (0x1A) bleibt erhalten / wird nach jedem Write neu angehängt.

Bekannte Interop-Grenze (Spec D-16): das Original nutzt CodeBase-Byte-Range-
Locks innerhalb der DBF-Dateien, als atomare Gruppensperre über Haupt- und
-L-Dateien aller Tabellen. Dieses Modul nutzt stattdessen POSIX-flock() je
Datei — die beiden Sperrschemata sehen einander nicht, und Daten- plus
Journal-Write sind als Paar nicht atomar. Gleichzeitiges Schreiben, während
ein Original-SP5-Client läuft, ist daher NICHT sicher; sequenzielle Koexistenz
(Original während der lib-Writes geschlossen) schon.
"""

import fcntl
import logging
import os
import struct
from contextlib import contextmanager
from datetime import date
from typing import Any

from .dbf_reader import (
    _compile_field_specs,
    _dedupe_names,
    _parse_record,
    _parse_record_specs,
    get_table_fields,
)

logger = logging.getLogger(__name__)

#: .CDX-Dateien einer geänderten Tabelle nach jedem erfolgreichen Write
#: löschen (siehe Modul-Docstring, „CDX-Indexdateien"). Interop-sicherer Default.
INVALIDATE_CDX = True

#: OPT-IN: die .CDX nach einem Write in place neu aufbauen statt zu löschen,
#: damit ein Original-SP5/CodeBase-Client die Tabelle OHNE Index-Neuaufbau
#: öffnet (ROADMAP §B.2). STANDARDMÄSSIG AUS — der bewährte Lösch-Pfad oben
#: bleibt der Default. Aktivierung via env SP5_CDX_WRITE=1 oder Flag True.
#: Aktiviert nutzt dies den Writer sp5lib.cdx_writer; kann der die
#: Schlüsselform der Tabelle nicht abbilden, fällt er aufs Löschen der
#: veralteten CDX zurück — nie schlechter als der Default. Reproduzierter/
#: verifizierter Umfang und Grenzen: siehe sp5lib/cdx_writer.py.
WRITE_CDX = os.environ.get("SP5_CDX_WRITE", "") == "1"

#: ASCII-Klasse-C-Felder (Spec D-20): reines cp1252, leerzeichengetrennte
#: Listen. STARTEND* (D-31) gehört zur selben Klasse. Der Namensabgleich ist
#: für das feste 30-Tabellen-Schema exakt (CATEGORY/REPORT existieren als
#: C-Felder nur in 5USER; die CATEGORY-Felder von 5SHIFT/5LEAVT sind N-typisiert).
_ASCII_C_FIELDS = {"WORKDAYS", "VALIDDAYS", "DAILYDEM", "CATEGORY", "REPORT"}


def _is_ascii_c_field(name: str) -> bool:
    return name in _ASCII_C_FIELDS or name.startswith("STARTEND")


# ─── String-/Feld-Kodierung ───────────────────────────────────────────────────


def _encode_string(value: str, field_len: int) -> bytes:
    """
    Encode a Python string to a Schichtplaner5 C field.

    Format: [UTF-16-LE bytes] [\\x00\\x00 null-terminator] [\\x20 padding …]
    For an empty string the result is [\\x00\\x00] [\\x20 …].
    """
    if field_len <= 0:
        return b""

    if not value:
        # empty string: just null-terminator + spaces
        if field_len >= 2:
            return b"\x00\x00" + b"\x20" * (field_len - 2)
        # Feld zu kurz für den Null-Terminator – einfach mit Nullen füllen
        return b"\x00" * field_len

    encoded = value.encode("utf-16-le")

    # 2 Bytes für den Null-Terminator freihalten (außer das Feld ist zu klein)
    max_content = max(0, field_len - 2)
    # An gerader Byte-Grenze abschneiden – Warnung, damit Datenverlust sichtbar ist
    if len(encoded) > max_content:
        max_chars = (max_content & ~1) // 2
        logger.warning(
            "DBF field truncation: value '%s...' (%d chars) exceeds field capacity "
            "(%d chars / %d bytes). Truncating silently.",
            value[:30],
            len(value),
            max_chars,
            max_content & ~1,
        )
        encoded = encoded[: max_content & ~1]

    null_term = b"\x00\x00" if field_len - len(encoded) >= 2 else b""
    padding = b"\x20" * (field_len - len(encoded) - len(null_term))
    result = encoded + null_term + padding

    # Safety: always return exactly field_len bytes
    if len(result) < field_len:
        result += b"\x20" * (field_len - len(result))
    return result[:field_len]


def _encode_field(value: Any, field: dict) -> bytes:
    """Kodiert einen Einzelwert gemäß seinem DBF-Felddeskriptor."""
    ftype = field["type"]
    flen = field["len"]
    fdec = field["dec"]

    if value is None:
        return b" " * flen

    if ftype == "C":
        # Binärfelder (Spec D-21): rohe Bytes, \x00-aufgefüllt.
        if isinstance(value, bytes):
            return (value + b"\x00" * flen)[:flen]
        if _is_ascii_c_field(field["name"]):
            # ASCII-class fields (Spec D-20/D-31): plain cp1252, space-padded.
            s = str(value) if value else ""
            encoded = s.encode("cp1252", errors="replace")
            if len(encoded) > flen:
                logger.warning(
                    "DBF ASCII field truncation: %s value '%s' exceeds %d bytes",
                    field["name"], s[:30], flen,
                )
                encoded = encoded[:flen]
            return encoded + b"\x20" * (flen - len(encoded))
        return _encode_string(str(value) if value else "", flen)

    elif ftype == "D":
        # Erwartet 'YYYY-MM-DD' oder 'YYYYMMDD'; leere Werte mit Leerzeichen aufgefüllt
        s = str(value).strip() if value else ""
        if len(s) == 10 and s[4] == "-":
            s = s.replace("-", "")  # YYYY-MM-DD → YYYYMMDD
        if len(s) == 8 and s.isdigit():
            return s.encode("ascii").ljust(flen)[:flen]
        return b" " * flen

    elif ftype in ("N", "F"):
        try:
            if ftype == "F":
                # Spec D-15: F-Felder tragen in den Originaldateien immer 4
                # Nachkommastellen ('7.7000'), obwohl der Deskriptor dec=0
                # sagt. Genau dieses Byte-Format nachbilden.
                s = f"{{:>{flen}.4f}}".format(float(value))
            elif fdec > 0:
                fmt = f"{{:>{flen}.{fdec}f}}"
                s = fmt.format(float(value))
            else:
                fmt = f"{{:>{flen}d}}"
                s = fmt.format(int(float(value)))
        except (ValueError, TypeError):
            return b" " * flen
        # Ein rechtsbündiges Zahlenformat schneidet überbreite Werte nicht ab —
        # Slicen würde still die *höchstwertigen* Ziffern/das Vorzeichen
        # verwerfen und den Betrag verfälschen (z. B. 99999 -> "9999").
        # Ablehnen statt korrumpieren.
        if len(s) > flen:
            raise ValueError(
                f"Numeric value {value!r} does not fit field "
                f"{field.get('name', '?')} (len={flen}, dec={fdec})"
            )
        return s.encode("ascii")

    elif ftype == "L":
        return b"T" if value else b"F"

    elif ftype == "M":
        return b" " * flen

    else:
        return str(value).ljust(flen).encode("ascii", errors="replace")[:flen]


# ─── header helpers ───────────────────────────────────────────────────────────


def _read_header_info(filepath: str) -> tuple[int, int, int]:
    """Liefert (num_records, header_size, record_size) aus dem DBF-Header."""
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"DBF-Datei nicht gefunden: {filepath}")
    with open(filepath, "rb") as f:
        hdr = f.read(32)
    if len(hdr) < 32:
        raise ValueError(f"Truncated DBF header: {filepath}")
    num_records = struct.unpack_from("<I", hdr, 4)[0]
    header_size = struct.unpack_from("<H", hdr, 8)[0]
    record_size = struct.unpack_from("<H", hdr, 10)[0]
    return num_records, header_size, record_size


def _stamp_header(f) -> None:
    """Schreibt das heutige Datum (JJ MM TT) in die Bytes 1–3 der offenen Datei."""
    today = date.today()
    f.seek(1)
    f.write(bytes([today.year % 100, today.month, today.day]))


def _update_record_count(f, new_count: int) -> None:
    """Schreibt die neue Satzanzahl in die Bytes 4–7 der offenen Datei."""
    f.seek(4)
    f.write(struct.pack("<I", new_count))


# ─── Datei-Sperren ────────────────────────────────────────────────────────────


@contextmanager
def _exclusive_open(filepath: str):
    """Öffnet filepath zum Lesen+Schreiben mit exklusivem POSIX-Lock."""
    with open(filepath, "r+b") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield f
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# ─── Änderungsjournal & Index-Invalidierung ───────────────────────────────────

#: Zusammengesetzte Journal-Schlüssel je Tabelle (Spec D-41): CHANGEID1..3 =
#: Schlüsselkomponenten. Nicht gelistete Tabellen haben den Ein-Komponenten-Schlüssel (ID).
_JOURNAL_KEYS: dict[str, tuple[str, ...]] = {
    "5ABSEN": ("ID", "EMPLOYEEID"),
    "5BOOK": ("ID", "EMPLOYEEID"),
    "5CYASS": ("ID", "EMPLOYEEID"),
    "5GRASG": ("ID", "EMPLOYEEID"),
    "5LEAEN": ("ID", "EMPLOYEEID"),
    "5MASHI": ("ID", "EMPLOYEEID"),
    "5NOTE": ("ID", "EMPLOYEEID"),
    "5OVER": ("ID", "EMPLOYEEID"),
    "5RESTR": ("ID", "EMPLOYEEID"),
    "5SPSHI": ("ID", "EMPLOYEEID"),
    "5DADEM": ("ID", "GROUPID"),
    "5HOBAN": ("ID", "GROUPID"),
    "5PERIO": ("ID", "GROUPID"),
    "5SHDEM": ("ID", "GROUPID"),
    "5SPDEM": ("ID", "GROUPID"),
    "5EMACC": ("ID", "USERID"),
    "5GRACC": ("ID", "USERID"),
    "5CYENT": ("ID", "CYCLEEID"),
    "5CYEXC": ("ID", "EMPLOYEEID", "CYCLEASSID"),
}


def _table_stem(filepath: str) -> str:
    return os.path.splitext(os.path.basename(filepath))[0]


def _is_journal_file(filepath: str) -> bool:
    return _table_stem(filepath).upper().endswith("-L")


def _journal_path(filepath: str) -> str | None:
    """Liefert den Pfad der -L-Begleittabelle, oder None wenn nicht vorhanden."""
    base, ext = os.path.splitext(filepath)
    for suffix in ("-L", "-l"):
        candidate = base + suffix + ext
        if os.path.exists(candidate):
            return candidate
    return None


def _invalidate_cdx(filepath: str) -> None:
    """Hält die .CDX der Tabelle nach einem Write konsistent zu den Daten.

    Default (interop-sicher): die veraltete .CDX LÖSCHEN, damit das Original
    sie beim nächsten Öffnen neu aufbaut (Spec D-14). Opt-in (``WRITE_CDX`` /
    env ``SP5_CDX_WRITE=1``): die .CDX über :mod:`sp5lib.cdx_writer` in place
    neu aufbauen, sodass das Original ohne Neuaufbau öffnet. Der optionale
    Writer fällt bei nicht abbildbarer Schlüsselform aufs Löschen zurück —
    nie schlechter als der Default.
    """
    if WRITE_CDX:
        from . import cdx_writer

        try:
            if cdx_writer.write_cdx(filepath) is not None:
                return  # rebuilt in place — done
        except (ValueError, OSError) as exc:
            logger.warning(
                "CDX rebuild failed for %s (%s) — falling back to invalidation",
                filepath,
                exc,
            )
        # No CDX present or unsupported key — fall through to deletion.

    if not INVALIDATE_CDX:
        return
    base, _ = os.path.splitext(filepath)
    for ext in (".CDX", ".cdx"):
        candidate = base + ext
        if os.path.exists(candidate):
            try:
                os.remove(candidate)
            except OSError as exc:
                logger.warning("Could not remove stale index %s: %s", candidate, exc)


def _append_journal(filepath: str, record: dict, change: int) -> None:
    """Hängt einen Änderungsjournal-Eintrag (Spec D-69/D-72) für *record* an.

    change: 1 = Satz angelegt/geändert (Upsert), 2 = Satz gelöscht.
    Fehlende -L-Datei: mit Warnung überspringen (Haupt-Write bleibt gültig).
    """
    jpath = _journal_path(filepath)
    if jpath is None:
        logger.warning(
            "DBF change journal missing for %s — entry skipped "
            "(running original clients will not see this change)",
            filepath,
        )
        return

    keys = _JOURNAL_KEYS.get(_table_stem(filepath).upper(), ("ID",))
    ids = [int(record.get(k) or 0) for k in keys]
    ids += [0] * (3 - len(ids))

    journal_record = {
        "NUMBER": 0,  # allocated atomically under the -L lock (autoid_field below)
        "CHANGEID1": ids[0],
        "CHANGEID2": ids[1],
        "CHANGEID3": ids[2],
        "CHANGE": change,
    }
    # append_record auf einer -L-Datei journalt nicht erneut (Guard _is_journal_file).
    # NUMBER wird atomar im Exklusiv-Lock von append_record vergeben, sodass zwei
    # nebenläufige Journal-Writes nicht auf derselben NUMBER kollidieren (gleiche Wurzel wie P0-1).
    # Eine korrupte oder nicht beschreibbare -L-Datei wird wie eine fehlende
    # behandelt (Warnung + überspringen): der Haupt-Write ist hier bereits
    # gelungen — ein Raise ließe den Aufrufer fälschlich von einem Fehlschlag ausgehen.
    try:
        append_record(
            jpath, get_table_fields(jpath), journal_record, autoid_field="NUMBER"
        )
    except (OSError, ValueError) as exc:
        logger.warning(
            "DBF change journal %s not writable (%s) — entry skipped "
            "(running original clients will not see this change)",
            jpath,
            exc,
        )


def _after_write(filepath: str, record: dict, change: int) -> None:
    """Post-write upkeep: journal entry (D-69) + index invalidation (D-14)."""
    if not _is_journal_file(filepath):
        _append_journal(filepath, record, change)
    _invalidate_cdx(filepath)


# ─── public API ───────────────────────────────────────────────────────────────


def _max_id_under_lock(
    f, names: list[str], fields: list[dict], header_size: int,
    record_size: int, num_records: int, id_field: str,
) -> int:
    """Höchster Wert von *id_field* über die aktiven Sätze der bereits
    geöffneten, bereits gesperrten Datei *f* (gelöschte Sätze übersprungen,
    analog read_dbf).

    Für die atomare ID-Vergabe innerhalb des Exklusiv-Locks von
    :func:`append_record`. Es werden nur die Bytes des ID-Felds dekodiert
    (nicht der ganze Satz) — der Scan bleibt auch bei großen Tabellen billig.
    """
    # Byte-Offset des ID-Felds im Satz (1 = führendes Lösch-Flag-Byte).
    offset = 1
    field_len = None
    for field, fname in zip(fields, names, strict=True):
        if fname == id_field:
            field_len = int(field["len"])
            break
        offset += int(field["len"])
    if field_len is None:
        raise ValueError(f"autoid field {id_field!r} not present in fields list")

    f.seek(header_size)
    area = f.read(num_records * record_size)
    max_id = 0
    for i in range(num_records):
        rec = area[i * record_size : (i + 1) * record_size]
        if len(rec) < record_size or rec[0] == 0x2A:
            continue  # truncated or deleted → not counted (matches read_dbf)
        raw = rec[offset : offset + field_len].strip()
        if raw:
            try:
                max_id = max(max_id, int(raw))
            except ValueError:
                pass  # non-numeric id field — ignore, defensive
    return max_id


def append_record(
    filepath: str, fields: list[dict], record: dict, autoid_field: str | None = None
) -> int:
    """
    Append *record* to the end of *filepath*.

    Parameters
    ----------
    filepath : str
        Path to the .DBF file.
    fields : list[dict]
        Field descriptors as returned by :func:`get_table_fields`.
    record : dict
        Mapping of field-name → value.  Missing fields default to None.
    autoid_field : str | None
        If given, the record's ID is assigned atomically **inside** the exclusive
        lock as ``max(existing) + 1`` and written back into *record* in place.
        This closes the lost-update race where two concurrent callers computed the
        same ``max(ID)+1`` outside any lock and wrote duplicate IDs — which made
        ID-addressed updates/deletes hit the wrong record under load (P0-1).
        Callers must read the assigned value back from ``record[autoid_field]``.

    Returns
    -------
    int
        New total record count after appending.
    """
    names = _dedupe_names([str(f["name"]) for f in fields])
    num_records, header_size, record_size = _read_header_info(filepath)

    with _exclusive_open(filepath) as f:
        # Satzanzahl im Lock erneut lesen (TOCTOU-Race vermeiden):
        # two concurrent appends might both read num_records=N before either
        # das Lock bekommt — beide schrieben sonst new_count=N+1 statt N+2.
        f.seek(4)
        num_records = struct.unpack("<I", f.read(4))[0]

        # Atomare ID-Vergabe unter DEMSELBEN Lock wie der Append (P0-1). Die
        # ID außerhalb des Locks zu berechnen ließ nebenläufige Writer identische IDs ziehen.
        if autoid_field is not None:
            record[autoid_field] = _max_id_under_lock(
                f, names, fields, header_size, record_size, num_records, autoid_field
            ) + 1

        # Rohe Satz-Bytes bauen (1 Aktiv-Flag-Byte + Felddaten); die ID ist
        # jetzt bekannt. Feldwerte werden per dedupliziertem Namen nachgeschlagen
        # (5DADEM: START/START2), passend zum Lesepfad.
        row = bytearray(b"\x20")  # delete-flag: active
        for field, fname in zip(fields, names, strict=True):
            row += _encode_field(record.get(fname), field)

        # Record-Size-Mismatch: eine zu lange Zeile stillschweigend abzuschneiden
        # würde Feldgrenzen verschieben und den Satz korrumpieren (falsche
        # fields-Liste oder beschädigter Header) — ablehnen statt korrumpieren.
        if len(row) > record_size:
            raise ValueError(
                f"Record size mismatch for {filepath}: encoded row is {len(row)} bytes, "
                f"header says record_size={record_size} — fields list does not match the file"
            )
        # Kürzere Zeilen werden aufgefüllt (defensiv für Header mit Rest-Slack).
        if len(row) < record_size:
            row += b"\x20" * (record_size - len(row))
        row_bytes: bytes = bytes(row)

        # Schreibposition finden: direkt vor dem EOF-Marker (0x1A), falls vorhanden
        f.seek(0, 2)  # seek to end
        file_end = f.tell()

        # Prüfen, ob das allerletzte Byte der EOF-Marker ist
        if file_end > 0:
            f.seek(-1, 2)
            last = f.read(1)
            if last == b"\x1a":
                f.seek(-1, 2)  # overwrite the marker
            else:
                f.seek(0, 2)  # append after whatever is there

        write_pos = f.tell()  # remember rollback point
        try:
            f.write(row_bytes)
            f.write(b"\x1a")  # re-append EOF marker

            new_count = num_records + 1
            _update_record_count(f, new_count)
            _stamp_header(f)
        except Exception:
            # Rollback-Versuch: auf den Zustand vor dem Write zurückkürzen, damit
            # die Datei nicht halb geschrieben zurückbleibt (z. B. bei voller Platte).
            try:
                f.truncate(write_pos)
                f.flush()
            except Exception as trunc_err:
                logger.error(
                    "DBF rollback failed after write error — file may be corrupted: "
                    "%s (truncate error: %s)",
                    filepath,
                    trunc_err,
                )
            raise

    _after_write(filepath, record, change=1)
    return new_count


def delete_record(filepath: str, fields: list[dict], record_index: int) -> None:
    """
    Mark record *record_index* as deleted by writing 0x2A at its first byte.

    Parameters
    ----------
    record_index : int
        Zero-based raw index (counting deleted records too), as returned
        by :func:`find_all_records`.
    """
    num_records, header_size, record_size = _read_header_info(filepath)

    if record_index < 0 or record_index >= num_records:
        raise IndexError(
            f"record_index {record_index} out of range (file has {num_records} records)"
        )

    byte_offset = header_size + record_index * record_size

    with _exclusive_open(filepath) as f:
        f.seek(byte_offset)
        raw = f.read(record_size)
        if not raw:
            raise ValueError(f"Record {record_index} could not be read (empty read)")
        if raw[0] == 0x2A:
            return  # already deleted – nothing to do
        f.seek(byte_offset)
        f.write(b"\x2a")
        _stamp_header(f)

    # Der Journaleintrag braucht die Schlüsselkomponenten des gelöschten Satzes (Spec D-69).
    _after_write(filepath, _parse_record(raw, fields), change=2)


def update_record(
    filepath: str,
    fields: list[dict],
    record_index: int,
    data: dict,
) -> None:
    """
    Overwrite specific fields of record *record_index* in-place.

    Parameters
    ----------
    filepath : str
        Path to the .DBF file.
    fields : list[dict]
        Field descriptors as returned by :func:`get_table_fields`.
    record_index : int
        Zero-based raw index (counting deleted records too), as returned
        by :func:`find_all_records`.
    data : dict
        Mapping of field-name → new-value.  Only listed fields are changed;
        all other fields are left untouched.
    """
    num_records, header_size, record_size = _read_header_info(filepath)

    if record_index < 0 or record_index >= num_records:
        raise IndexError(
            f"record_index {record_index} out of range (file has {num_records} records)"
        )

    byte_offset = header_size + record_index * record_size

    # Lesen UND Schreiben unter demselben Exklusiv-Lock (TOCTOU-Race verhindern).
    with _exclusive_open(filepath) as f:
        f.seek(byte_offset)
        raw = bytearray(f.read(record_size))

        if not raw:
            raise ValueError(f"Record {record_index} could not be read (empty read)")

        if raw[0] == 0x2A:
            raise ValueError(f"Record {record_index} is already deleted")

        # Nur die angeforderten Felder überschreiben (Schlüssel sind deduplizierte
        # Namen, 5DADEM: START/START2 — passend zum Lesepfad)
        names = _dedupe_names([str(f_["name"]) for f_ in fields])
        offset = 1  # skip delete-flag byte
        for field, fname in zip(fields, names, strict=True):
            if fname in data:
                encoded = _encode_field(data[fname], field)
                raw[offset : offset + field["len"]] = encoded
            offset += field["len"]

        f.seek(byte_offset)
        f.write(bytes(raw))
        _stamp_header(f)

    _after_write(filepath, _parse_record(bytes(raw), fields), change=1)


def _zap_journal(filepath: str) -> None:
    """Leert das -L-Änderungsjournal von *filepath* (Spec D-74).

    „Komprimieren" leert das Journal über den gesamten Nummernbereich und
    setzt den Zähler auf 0; Original-Clients erkennen das und laden voll neu
    (CHANGE=0-Semantik). Fehlende -L-Datei: nichts zu tun.
    """
    jpath = _journal_path(filepath)
    if jpath is None:
        return
    _num, header_size, _record_size = _read_header_info(jpath)
    with _exclusive_open(jpath) as f:
        f.truncate(header_size)
        f.seek(header_size)
        f.write(b"\x1a")  # EOF marker directly after the header
        _update_record_count(f, 0)
        _stamp_header(f)
    _invalidate_cdx(jpath)


def pack_table(filepath: str) -> int:
    """PACK *filepath*: physically remove deleted records (Spec 1.14/D-11).

    Rewrites the record area without the records flagged ``0x2A``, updates
    the header record count and date stamp and re-appends the EOF marker —
    all under an exclusive lock. Afterwards the table's -L change journal is
    zapped (counter reset to 0, Spec D-74) and the stale CDX files of the
    main table and the journal are deleted (D-14), because record positions
    have changed. If the table contains no deleted records, the file is left
    untouched. Returns the number of physically removed records.
    """
    num_records, header_size, record_size = _read_header_info(filepath)
    if record_size <= 0:
        return 0

    removed = 0
    with _exclusive_open(filepath) as f:
        # Anzahl im Lock erneut lesen (TOCTOU, vgl. append_record)
        f.seek(4)
        num_records = struct.unpack("<I", f.read(4))[0]
        f.seek(header_size)
        records_area = f.read(num_records * record_size)
        kept = []
        for i in range(num_records):
            rec = records_area[i * record_size : (i + 1) * record_size]
            if len(rec) < record_size:
                break  # truncated trailing record — drop it
            if rec[0:1] == b"\x2a":
                removed += 1
            else:
                kept.append(rec)
        if removed == 0 and len(kept) == num_records:
            return 0  # nothing to pack — keep file, journal and CDX intact

        f.seek(header_size)
        for rec in kept:
            f.write(rec)
        f.write(b"\x1a")  # EOF marker
        f.truncate()
        _update_record_count(f, len(kept))
        _stamp_header(f)

    if not _is_journal_file(filepath):
        _zap_journal(filepath)
    _invalidate_cdx(filepath)
    return removed


def find_all_records(
    filepath: str,
    fields: list[dict] | None = None,
    **filters,
) -> list[tuple[int, dict]]:
    """
    Return every non-deleted record in *filepath* that matches all *filters*.

    Parameters
    ----------
    filepath : str
        Path to the .DBF file.
    fields : list[dict] | None
        Field descriptors.  Loaded automatically if not supplied.
    **filters :
        Keyword arguments specifying field → expected-value pairs.
        All must match (AND semantics).

    Returns
    -------
    list[tuple[int, dict]]
        Each tuple is (raw_record_index, record_dict).
        *raw_record_index* is the 0-based index in the file (counting deleted
        records too) and can be passed directly to :func:`delete_record`.
    """
    if not os.path.exists(filepath):
        return []

    if fields is None:
        fields = get_table_fields(filepath)

    try:
        num_records, header_size, record_size = _read_header_info(filepath)
    except (FileNotFoundError, OSError, ValueError):
        # Datei zwischen Existenz-Check und open entfernt oder korrupt
        return []

    results: list[tuple[int, dict]] = []

    try:
        open_file = open(filepath, "rb")
    except OSError:
        return []

    # Feld-Spezifikationen EINMAL je Aufruf kompilieren statt je Datensatz —
    # auf großen Tabellen (5MASHI mit >20k Sätzen) halbiert das den Scan.
    names = _dedupe_names([str(f["name"]) for f in fields])
    specs = _compile_field_specs(fields, names)

    with open_file as f:
        # Shared (read) lock
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            f.seek(header_size)
            for raw_idx in range(num_records):
                raw = f.read(record_size)
                if not raw or len(raw) < record_size:
                    break
                if raw[0] == 0x2A:
                    continue  # deleted

                record = _parse_record_specs(raw, specs)

                if _matches(record, filters):
                    results.append((raw_idx, record))
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    return results


def _matches(record: dict, filters: dict) -> bool:
    """Liefert True, wenn *record* alle key=value-Paare aus *filters* erfüllt."""
    for key, expected in filters.items():
        if record.get(key) != expected:
            return False
    return True
