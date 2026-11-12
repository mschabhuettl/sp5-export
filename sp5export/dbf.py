"""Strict reading of one SP5 table (DBF).

Unlike sp5lib.dbf_reader.read_dbf, which returns an empty list for anything it cannot read, every problem
is an error here: an unreadable file, a damaged header, a record count in the header that does not match
the file, an unknown code page. Text fields that are not UTF-16 are decoded with the code page of the
header (byte 29); field parsing is otherwise the one of sp5lib.
"""

import struct
from pathlib import Path

from sp5lib.dbf_reader import _compile_field_specs, _dedupe_names, _is_utf16_le, _parse_record_specs

# dBASE language driver ids (header byte 29) and their code pages. 0 means "not set"; such files are read
# as before (Latin-1, every byte kept).
CODE_PAGES = {
    0x00: "latin-1", 0x01: "cp437", 0x02: "cp850", 0x03: "cp1252", 0x57: "cp1252", 0x58: "cp1252",
    0x59: "cp1252", 0x64: "cp852", 0x65: "cp866", 0x66: "cp865", 0x67: "cp861", 0x6A: "cp737", 0x6B: "cp857",
    0xC8: "cp1250", 0xC9: "cp1251", 0xCA: "cp1254", 0xCB: "cp1253",
}
EOF_MARK = 0x1A
DELETED = 0x2A


class DbfError(Exception):
    """The table cannot be read completely. The message is German and names no field values."""


def read_table(path: Path) -> list[dict]:
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise DbfError(f"nicht lesbar ({type(e).__name__})") from None
    if len(data) < 32:
        raise DbfError("beschädigt (Dateikopf unvollständig)")
    count, header_size, record_size = struct.unpack_from("<IHH", data, 4)
    fields = []
    for pos in range(32, min(header_size, len(data)) - 31, 32):
        desc = data[pos:pos + 32]
        if desc[0] == 0x0D:
            break
        fields.append({"name": desc[0:11].split(b"\x00")[0].decode("ascii", errors="replace").strip(),
                       "type": chr(desc[11]), "len": desc[16], "dec": desc[17]})
    if not fields or record_size != 1 + sum(f["len"] for f in fields):
        raise DbfError("beschädigt (Feldbeschreibung passt nicht zur Satzlänge)")
    end = header_size + count * record_size
    if len(data) < end:
        complete = max(0, len(data) - header_size) // record_size
        raise DbfError(f"unvollständig: laut Dateikopf {count} Datensätze, vorhanden {complete}")
    if len(data) > end and data[end] != EOF_MARK:
        raise DbfError(f"laut Dateikopf {count} Datensätze, die Datei enthält mehr")
    encoding = CODE_PAGES.get(data[29])
    if encoding is None:
        raise DbfError(f"unbekannte Codepage (Dateikopf Byte 29 = 0x{data[29]:02X})")

    specs = _compile_field_specs(fields, _dedupe_names([f["name"] for f in fields]))
    texts = [(name, offset, length) for name, ftype, length, _, binary, offset in specs if ftype == "C" and not binary]
    records = []
    for i in range(count):
        raw = data[header_size + i * record_size:header_size + (i + 1) * record_size]
        if raw[0] == DELETED:
            continue
        record = _parse_record_specs(raw, specs)
        for name, offset, length in texts:
            chunk = raw[offset:offset + length]
            # sp5lib's heuristic misses one-character UTF-16 text ("F\0\0\0  "). Its second byte is a UTF-16
            # high byte (below 0x08), which code-page text never has.
            if len(chunk) >= 2 and chunk[1] < 0x08 and chunk[:2] != b"\0\0" and not _is_utf16_le(chunk):
                record[name] = chunk[:2].decode("utf-16-le").strip()
            elif not _is_utf16_le(chunk):
                record[name] = chunk.rstrip(b"\x00\x20").decode(encoding, errors="replace").strip()
        records.append(record)
    return records
