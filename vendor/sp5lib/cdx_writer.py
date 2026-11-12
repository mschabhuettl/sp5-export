"""
OPTIONALER FoxPro-Verbundindex-(.CDX)-WRITER für Schichtplaner5-Tabellen.

Dieses Modul ist STANDARDMÄSSIG AUS. Das bewährte, interop-sichere Verhalten
von ``dbf_writer`` ist, die veraltete ``.CDX`` einer Tabelle nach jedem Write
zu LÖSCHEN, damit der Original-Schichtplaner5/CodeBase-Client sie beim
nächsten Öffnen neu aufbaut (Spec D-13/D-14). Der Writer hier ist die von
ROADMAP §B.2 gewünschte Opt-in-Alternative: er baut die ``.CDX`` aus dem
aktuellen ``.DBF``-Inhalt in place neu, sodass das Original die Tabelle OHNE
Neuaufbau öffnet. Aktiv nur bei ``dbf_writer.WRITE_CDX`` True (env
``SP5_CDX_WRITE=1``); sonst wird dieses Modul nie erreicht.

Format (byteweise aus den Beispiel-DB-CDX-Dateien im META-Repo reverse-
engineert, gegengeprüft mit dem öffentlichen FoxPro-CDX-Layout)
--------------------------------------------------------------------------
Eine CDX ist eine Folge von 512-Byte-Seiten. Seite 0 ist der Index-Header des
*Tag-Listen*-B-Baums (ein B-Baum, dessen Schlüssel Tag-Namen → Byte-Offset des
jeweiligen Tag-Index-Headers sind). Jeder Tag hat dann einen eigenen
Index-Header, eine Expression-Pool-Seite (Schlüssel- + FOR-Ausdruckstext) und
eine oder mehrere B-Baum-Knotenseiten (innere + Blätter).

Jede Schichtplaner5-Haupttabelle führt genau EINEN Tag namens ``ID_TAG`` mit
dem FOR-Filter ``.NOT. DELETED()`` und einer von zwei Schlüsselformen:

  * einzelner Integer-Schlüssel ``ID``  → Schlüssellänge 8, FoxPro-*numerischer*
    Schlüssel (IEEE-Double des Werts, big-endian, High-Bit gekippt, damit die
    Byte-Reihenfolge sortiert);
  * zusammengesetzt ``STR(ID,11)+STR(<F>,11)[+STR(<F>,11)]`` → Schlüssellänge
    22 oder 33, *Zeichen*-Schlüssel (jeder Integer rechtsbündig,
    leerzeichen-aufgefüllt, in 11er-Spalten).

Index-Header-Seitenlayout (die Bytes, die dieser Writer erzeugt):
  0x00  uint32   Byte-Offset des Wurzelknotens
  0x04  int32    Freie-Knoten-Liste (-1 / 0 hier)
  0x08  uint32   Schlüssel-Operationszähler, BIG-endian (siehe „Umfang/Grenzen")
  0x0C  uint16   Schlüssellänge
  0x0E  uint8    Index-Optionen: 0x20 kompakt | 0x40 verbund | 0x08 mit-FOR
  0x0F  uint8    Signatur (1)
  0x1F6 uint16   0 = aufsteigend
  0x1F8 uint16   FOR-Ausdrucks-Offset im Pool = len(key_expr)+1
  0x1FA uint16   16 (Marker: FOR-Klausel vorhanden)
  0x1FC uint16   0
  0x1FE uint16   len(key_expr)+1  (Duplikat)
Die Expression-Pool-Seite direkt nach dem Header enthält
``<key_expr>\x00<for_expr>\x00``, mit Nullen auf 512 Bytes aufgefüllt.

Knotenseiten-Layout (die ersten 12 Bytes sind der Knoten-Header):
  0x00  uint16   Attribut: Bit0 Wurzel, Bit1 Blatt, Bit2 innerer Knoten
                 (0x03 Wurzel+Blatt, 0x02 Blatt, 0x01 innerer)
  0x02  uint16   Anzahl Schlüssel auf der Seite
  0x04  int32    Offset linker Nachbar (-1 keiner)
  0x08  int32    Offset rechter Nachbar (-1 keiner)
Innere Knoten speichern danach unkomprimiert ``nkeys`` Einträge aus
``key[keylen] + recno[4 BE] + child_offset[4 BE]``; der Separator-Schlüssel
ist der LETZTE Schlüssel des Kind-Teilbaums.
Blattknoten nutzen die kompakte FoxPro-Kompression. Nach dem 12-Byte-Header:
  0x0C  uint16   freie Bytes
  0x0E  uint32   Satznummern-Maske
  0x12  uint8    Duplikat-Zähler-Maske
  0x13  uint8    Trailing-Zähler-Maske
  0x14  uint8    Bits für die Satznummer
  0x15  uint8    Bits für den Duplikat-Zähler
  0x16  uint8    Bits für den Trailing-Zähler
  0x17  uint8    Bytes je Indexeintrag (3)
Jeder ``entry_bytes``-Eintrag packt ``recno | dup<<recno_bits |
trail<<(recno_bits+dup_bits)``. Die (unkomprimierten) Schlüsselzeichen liegen
vom SEITENENDE rückwärts: jeder Schlüssel liefert ``keylen - dup - trail``
frische Bytes, wobei ``dup`` mit dem Präfix des Vorgänger-Schlüssels geteilt
wird und ``trail`` abgeschnittene Füll-Bytes am Ende sind (0x20 bei Zeichen-,
0x00 bei numerischen Schlüsseln). ``recno`` ist die 1-basierte DBF-Satznummer.

Umfang / Grenzen (REGEL 5 — Byte-Parität)
-----------------------------------------
Byte-exakt für alles AUSSER den 4 Bytes bei 0x08 (Schlüssel-Operationszähler).
Dieser Zähler ist eine CodeBase-interne laufende Summe der Insert/Delete-
Operationen über die gesamte Lebenszeit der Tabelle (die Beispiel-``5MASHI.CDX``
trägt z. B. 194, obwohl die Tabelle aktuell 0 Sätze hat); er ist aus dem
aktuellen Tabelleninhalt NICHT ableitbar, und das Original nutzt ihn beim
Lesen des Index nicht. Dieser Writer ERHÄLT daher beim In-place-Neuaufbau den
Zähler der bestehenden CDX und fällt beim Neuaufbau von Grund auf auf die
aktuelle Satzanzahl zurück. Jedes andere Byte von Header, Expression-Pool,
Tag-Liste und B-Baum-Knoten wird exakt reproduziert. Verifizierte Fälle
(Wine + eigener Round-Trip): Ein-Blatt numerisch (``ID``) und zusammengesetzt-
Zeichen (``STR``) sowie der mehrseitige B-Baum (innere Knoten + mehrere
Blätter, z. B. ``5HOLID``).
"""

import os
import struct

from .dbf_reader import _dedupe_names, get_table_fields, read_dbf

PAGE = 512

# Feste Offsets, die sich im Ein-Tag-SP5-Schema nie ändern.
_TAGLIST_HDR_PAGE = 0  # page 0
_TAGLIST_ROOT_PAGE = 4  # 0x800
_TAG_HDR_PAGE = 2  # 0x400
_TAG_EXPR_PAGE = 3  # 0x600
_TAG_ROOT_PAGE = 5  # 0xa00 — first tag B-tree node

_FOR_EXPR = ".NOT. DELETED()"
_TAG_NAME = "ID_TAG"
_TAGLIST_KEYLEN = 10


# ─── Schlüsselaufbau ───────────────────────────────────────────────────────────


def _parse_key_expr(key_expr: str) -> tuple[str, object]:
    """Parst einen Schichtplaner5-``ID_TAG``-/``-L``-Journal-Schlüsselausdruck.

    Liefert eines von:
      * ``("numeric", field_name)`` für ein einzelnes numerisches Feld, z. B.
        ``ID`` (Haupttabellen) oder ``NUMBER`` (die ``-L``-Änderungsjournale);
      * ``("char", [(field, width), ...])`` für einen zusammengesetzten
        ``STR(ID,11)+STR(X,11)[+STR(Y,11)]``-Zeichen-Schlüssel.
    Wirft ``ValueError`` für alles außerhalb dieser Grammatik.
    """
    expr = key_expr.strip()
    if "+" not in expr and "(" not in expr:
        if not expr.isidentifier():
            raise ValueError(f"Unsupported CDX key expression: {expr!r}")
        return ("numeric", expr)
    parts = [p.strip() for p in expr.split("+")]
    out: list[tuple[str, int]] = []
    for p in parts:
        if not (p.startswith("STR(") and p.endswith(")")):
            raise ValueError(f"Unsupported CDX key expression term: {p!r}")
        inner = p[4:-1]
        field, width = inner.split(",")
        out.append((field.strip(), int(width)))
    return ("char", out)


def _numeric_key(value: float) -> bytes:
    """Numerischer FoxPro-8-Byte-Schlüssel: IEEE-Double, big-endian, vorzeichengefaltet."""
    b = bytearray(struct.pack(">d", float(value)))
    if b[0] & 0x80:  # negative: invert all bits so they sort below positives
        for i in range(8):
            b[i] ^= 0xFF
    else:  # positive/zero: set the high bit so they sort above negatives
        b[0] |= 0x80
    return bytes(b)


def _char_key(record: dict, terms: list[tuple[str, int]]) -> bytes:
    """Zusammengesetzter Zeichen-Schlüssel: jedes Feld als rechtsbündiges STR(field,width)."""
    out = b""
    for field, width in terms:
        val = record.get(field)
        ival = int(val or 0)
        out += str(ival).rjust(width).encode("ascii")[:width]
    return out


def _build_keys(filepath: str, key_expr: str) -> tuple[list[tuple[bytes, int]], int, bytes]:
    """Liefert ``(sortierte [(key, recno)], keylen, pad_byte)`` der Tabelle.

    ``recno`` ist die 1-basierte DBF-Satznummer (Rohposition + 1). Gelöschte
    Sätze sind ausgenommen (der FOR-Filter ist ``.NOT. DELETED()``). Die
    Schlüssel kommen aufsteigend sortiert — so läuft sie auch das Original ab.
    """
    fields = get_table_fields(filepath)
    names = _dedupe_names([str(f["name"]) for f in fields])
    kind, spec = _parse_key_expr(key_expr)

    if kind == "numeric":
        keylen, pad = 8, b"\x00"
    else:  # char
        keylen, pad = sum(w for _, w in spec), b"\x20"

    # Rohe Sätze mit physischen Positionen lesen (read_dbf lässt gelöschte
    # Zeilen weg, verliert dabei aber die Position — hier positional neu lesen).
    from .dbf_reader import _parse_record
    from .dbf_writer import _read_header_info

    num_records, header_size, record_size = _read_header_info(filepath)
    keys: list[tuple[bytes, int]] = []
    with open(filepath, "rb") as f:
        f.seek(header_size)
        for raw_idx in range(num_records):
            raw = f.read(record_size)
            if not raw or len(raw) < record_size:
                break
            if raw[0] == 0x2A:  # deleted → excluded by .NOT. DELETED()
                continue
            record = _parse_record(raw, fields, names)
            recno = raw_idx + 1  # CodeBase record numbers are 1-based
            if kind == "numeric":
                key = _numeric_key(record.get(spec) or 0)
            else:
                key = _char_key(record, spec)
            keys.append((key, recno))

    keys.sort(key=lambda kr: (kr[0], kr[1]))
    return keys, keylen, pad


# ─── Blatt-/Innerer-Knoten-Kodierung ───────────────────────────────────────────


def _entry_bit_layout(keylen: int) -> tuple[int, int, int, int, int, int]:
    """Liefert (recno_bits, dup_bits, trail_bits, recno_mask, dup_mask, trail_mask).

    Gegen jede Beispieltabelle verifiziert: die dup/trail-Bits ergeben
    ``keylen 8 -> 4, 22 -> 5, 33 -> 6``, die Satznummer bekommt die
    verbleibenden Bits des 3-Byte-Eintrags.
    """
    if keylen <= 15:
        cnt_bits = 4
    elif keylen <= 31:
        cnt_bits = 5
    else:
        cnt_bits = 6
    recno_bits = 24 - 2 * cnt_bits
    recno_mask = (1 << recno_bits) - 1
    cnt_mask = (1 << cnt_bits) - 1
    return recno_bits, cnt_bits, cnt_bits, recno_mask, cnt_mask, cnt_mask


def _compress(key: bytes, prev: bytes, keylen: int, pad: bytes) -> tuple[int, int]:
    """Liefert ``(dup, trail)`` für *key* relativ zu *prev*.

    ``dup`` ist die Zahl der mit dem Vorgänger-Schlüssel geteilten führenden
    Bytes. ``trail`` ist die Zahl der abwerfbaren Füll-Bytes am Ende — aber NUR
    bei Zeichen-Schlüsseln (``pad == b" "``). Numerische (Double-)Schlüssel
    werden in den Beispieldateien nie trailing-getrimmt (ihre 0x00-Bytes am
    Ende bleiben), ``trail`` bleibt dort also 0.
    """
    dup = 0
    while dup < keylen and dup < len(prev) and prev[dup] == key[dup]:
        dup += 1
    trail = 0
    if pad == b"\x20":
        while trail < (keylen - dup) and key[keylen - 1 - trail : keylen - trail] == pad:
            trail += 1
    return dup, trail


def _split_into_leaves(
    keys: list[tuple[bytes, int]], keylen: int, pad: bytes
) -> list[list[tuple[bytes, int]]]:
    """Packt Schlüssel gierig nach realer komprimierter Bytegröße in Blätter.

    Ein Blatt nimmt Einträge auf, solange ``24 + nkeys*3 + chars <= 512``. Die
    Kompression wird an jeder Blattgrenze neu berechnet, weil der erste
    Schlüssel eines neuen Blatts kein Präfix mit dem letzten Schlüssel des
    Vorgänger-Blatts teilt (``dup`` beginnt wieder bei 0). Das reproduziert den
    49/47-Split des Originals für die 96-Schlüssel-Tabelle ``5HOLID``.
    """
    pages: list[list[tuple[bytes, int]]] = []
    cur: list[tuple[bytes, int]] = []
    chars = 0
    prev = b""
    for key, recno in keys:
        dup, trail = _compress(key, prev, keylen, pad)
        fresh = keylen - dup - trail
        if cur and 24 + (len(cur) + 1) * 3 + chars + fresh > PAGE:
            pages.append(cur)
            cur = []
            prev = b""
            dup, trail = _compress(key, prev, keylen, pad)
            fresh = keylen - dup - trail
            chars = 0
        cur.append((key, recno))
        chars += fresh
        prev = key
    if cur:
        pages.append(cur)
    return pages


def _encode_leaf(
    keys: list[tuple[bytes, int]],
    keylen: int,
    pad: bytes,
    attr: int,
    left: int,
    right: int,
) -> bytes:
    """Kodiert eine kompakte Blattseite aus ``keys`` (bereits sortiert)."""
    recno_bits, dup_bits, trail_bits, recno_mask, dup_mask, trail_mask = _entry_bit_layout(
        keylen
    )
    entry_bytes = 3
    nkeys = len(keys)

    entries = bytearray()
    fresh_chunks = []  # per-key fresh bytes, in key order
    prev = b""
    for key, recno in keys:
        dup, trail = _compress(key, prev, keylen, pad)
        fresh = key[dup : keylen - trail]
        val = (
            (recno & recno_mask)
            | ((dup & dup_mask) << recno_bits)
            | ((trail & trail_mask) << (recno_bits + dup_bits))
        )
        entries += val.to_bytes(entry_bytes, "little")
        fresh_chunks.append(fresh)
        prev = key

    # Schlüsselzeichen wachsen vom SEITENENDE rückwärts in Schlüsselreihenfolge:
    # die frischen Bytes von key0 belegen den letzten Slot, key1 direkt davor
    # usw. Das low→high-Layout ist also die umgekehrte, konkatenierte Chunk-Liste.
    chars = b"".join(reversed(fresh_chunks))

    page = bytearray(PAGE)
    struct.pack_into("<H", page, 0, attr)
    struct.pack_into("<H", page, 2, nkeys)
    struct.pack_into("<i", page, 4, left)
    struct.pack_into("<i", page, 8, right)

    prologue_end = 24 + len(entries)
    free = PAGE - prologue_end - len(chars)
    struct.pack_into("<H", page, 12, free)
    struct.pack_into("<I", page, 14, recno_mask)
    page[18] = dup_mask
    page[19] = trail_mask
    page[20] = recno_bits
    page[21] = dup_bits
    page[22] = trail_bits
    page[23] = entry_bytes
    page[24 : 24 + len(entries)] = entries
    # Schlüsselzeichen wachsen rückwärts vom Seitenende
    page[PAGE - len(chars) : PAGE] = chars
    return bytes(page)


def _encode_interior(
    separators: list[tuple[bytes, int, int]], keylen: int, attr: int
) -> bytes:
    """Kodiert einen inneren Knoten. ``separators`` = [(last_key, last_recno, child_off)]."""
    page = bytearray(PAGE)
    struct.pack_into("<H", page, 0, attr)
    struct.pack_into("<H", page, 2, len(separators))
    struct.pack_into("<i", page, 4, -1)
    struct.pack_into("<i", page, 8, -1)
    off = 12
    for key, recno, child in separators:
        page[off : off + keylen] = key.ljust(keylen, b"\x00")[:keylen]
        off += keylen
        struct.pack_into(">I", page, off, recno)  # recno big-endian
        off += 4
        struct.pack_into(">I", page, off, child)  # child offset big-endian
        off += 4
    return bytes(page)


# ─── index-header pages ────────────────────────────────────────────────────────


def _encode_index_header(
    root_offset: int,
    counter: int,
    keylen: int,
    options: int,
    key_expr: str | None,
) -> bytes:
    """Kodiert eine 512-Byte-Index-Header-Seite (Tag-Liste oder ein Tag)."""
    page = bytearray(PAGE)
    struct.pack_into("<I", page, 0, root_offset)
    struct.pack_into("<i", page, 4, 0)  # free node list
    struct.pack_into(">I", page, 8, counter)  # key-op counter, big-endian
    struct.pack_into("<H", page, 12, keylen)
    page[14] = options
    page[15] = 1  # signature
    if key_expr is not None:
        klen1 = len(key_expr) + 1
        struct.pack_into("<H", page, 0x1F6, 0)
        struct.pack_into("<H", page, 0x1F8, klen1)
        struct.pack_into("<H", page, 0x1FA, 16)
        struct.pack_into("<H", page, 0x1FC, 0)
        struct.pack_into("<H", page, 0x1FE, klen1)
    else:
        # Tag-list header trailer (constant across all sample files).
        struct.pack_into("<H", page, 0x1F6, 0)
        struct.pack_into("<H", page, 0x1F8, 1)
        struct.pack_into("<H", page, 0x1FA, 1)
        struct.pack_into("<H", page, 0x1FC, 0)
        struct.pack_into("<H", page, 0x1FE, 1)
    return bytes(page)


def _encode_taglist_leaf(tag_name: str) -> bytes:
    """Das Wurzelblatt der Tag-Liste: ein Schlüssel *tag_name* → Tag-Header 0x400.

    Ein kompaktes Blatt mit einem einzigen Eintrag, der auf den Tag-Header
    (Seite 2) zeigt. Tag-Namen werden mit Leerzeichen auf die 10-Byte-
    Schlüssellänge der Tag-Liste aufgefüllt; die Auffüllung wird wie bei
    Datenblättern über ``trail`` komprimiert — das 6-Zeichen-``ID_TAG``
    (trail 4) und das 10-Zeichen-``NUMBER_TAG`` (trail 0) werden beide
    byte-exakt reproduziert.
    """
    name = tag_name.encode("ascii")[:_TAGLIST_KEYLEN]
    fresh = len(name)
    trail = _TAGLIST_KEYLEN - fresh  # spaces compressed off the end

    page = bytearray(PAGE)
    struct.pack_into("<H", page, 0, 0x0003)  # root+leaf
    struct.pack_into("<H", page, 2, 1)  # 1 key
    struct.pack_into("<i", page, 4, -1)
    struct.pack_into("<i", page, 8, -1)
    free = PAGE - (24 + 3) - fresh
    struct.pack_into("<H", page, 12, free)
    struct.pack_into("<I", page, 14, 0xFFFF)  # recno mask (16 bits)
    page[18] = 0x0F
    page[19] = 0x0F
    page[20] = 0x10  # 16 recno bits
    page[21] = 0x04
    page[22] = 0x04
    page[23] = 0x03  # entry bytes
    # entry: recno = tag header byte offset (0x400 = page 2), dup=0, trail.
    val = (_TAG_HDR_PAGE * PAGE) | (0 << 16) | (trail << 20)
    page[24:27] = val.to_bytes(3, "little")
    # frische Tag-Namen-Zeichen ganz am Seitenende
    page[PAGE - fresh : PAGE] = name
    return bytes(page)


# ─── B-Baum-Zusammenbau ────────────────────────────────────────────────────────


def _build_tag_btree(
    keys: list[tuple[bytes, int]], keylen: int, pad: bytes
) -> tuple[list[bytes], int]:
    """Baut die B-Baum-Knotenseiten des Tags.

    Liefert ``(pages, root_page_index)``; ``pages[0]`` liegt bei
    ``_TAG_ROOT_PAGE``, der Rest folgt. Passt alles auf eine Seite, entsteht
    ein einzelnes Wurzel+Blatt; sonst Blätter + ein innerer Wurzelknoten.
    """
    chunks = _split_into_leaves(keys, keylen, pad)
    if len(chunks) <= 1:
        # Einzelne Wurzel+Blatt-Seite.
        leaf = _encode_leaf(keys, keylen, pad, attr=0x0003, left=-1, right=-1)
        return [leaf], _TAG_ROOT_PAGE

    # In Blattseiten teilen, dann eine innere Wurzel darüber bauen.
    # Blattseiten belegen _TAG_ROOT_PAGE, _TAG_ROOT_PAGE+1, …; die innere
    # Wurzel kommt als LETZTE Seite (wie im Beispiel 5HOLID, wo die Wurzel die
    # letzte Seite ist). Nachbar-Links verketten die Blätter links↔rechts.
    n_leaves = len(chunks)
    leaf_start = _TAG_ROOT_PAGE
    root_page = leaf_start + n_leaves
    pages: list[bytes] = []
    separators: list[tuple[bytes, int, int]] = []
    for i, chunk in enumerate(chunks):
        page_idx = leaf_start + i
        left = (page_idx - 1) * PAGE if i > 0 else -1
        right = (page_idx + 1) * PAGE if i < n_leaves - 1 else -1
        pages.append(_encode_leaf(chunk, keylen, pad, attr=0x0002, left=left, right=right))
        last_key, last_recno = chunk[-1]
        separators.append((last_key, last_recno, page_idx * PAGE))
    interior = _encode_interior(separators, keylen, attr=0x0001)
    pages.append(interior)
    return pages, root_page


def build_cdx_bytes(
    filepath: str, key_expr: str, counter: int, tag_name: str = _TAG_NAME
) -> bytes:
    """Baut das vollständige ``.CDX``-Byte-Abbild für den einzelnen Tag.

    *counter* landet in Header-Byte 0x08 (CodeBase-Operationszähler); Aufrufer
    übergeben den erhaltenen Originalwert oder einen Fallback. *tag_name* ist
    der Tag-Name (``ID_TAG`` für Haupttabellen, ``NUMBER_TAG`` für ``-L``-Journale).
    """
    keys, keylen, pad = _build_keys(filepath, key_expr)
    options = 0x68  # has-FOR | compact | compound

    tag_pages, tag_root_page = _build_tag_btree(keys, keylen, pad)

    # Seite 0: Index-Header der Tag-Liste (Wurzel = Tag-Listen-Blatt auf Seite 4).
    taglist_hdr = _encode_index_header(
        _TAGLIST_ROOT_PAGE * PAGE, counter, _TAGLIST_KEYLEN, options=0xE0, key_expr=None
    )
    # Seite 1: ungenutzt (nur Nullen) — in jeder Beispieldatei vorhanden.
    page1 = b"\x00" * PAGE
    # Seite 2: der Index-Header des Tags (Wurzel = B-Baum-Wurzelseite des Tags).
    # Der Operationszähler lebt nur im Tag-Listen-Header (Seite 0); der
    # Tag-Header trägt in jeder Beispieldatei 0.
    tag_hdr = _encode_index_header(
        tag_root_page * PAGE, 0, keylen, options=options, key_expr=key_expr
    )
    # Seite 3: Expression-Pool.
    expr_pool = bytearray(PAGE)
    blob = key_expr.encode("ascii") + b"\x00" + _FOR_EXPR.encode("ascii") + b"\x00"
    expr_pool[: len(blob)] = blob
    # Page 4: tag-list root leaf.
    taglist_leaf = _encode_taglist_leaf(tag_name)

    pages = [taglist_hdr, page1, tag_hdr, bytes(expr_pool), taglist_leaf]
    pages += tag_pages
    return b"".join(pages)


# ─── Öffentlicher Einstiegspunkt ───────────────────────────────────────────────


def _read_counter(cdx_path: str, default: int) -> int:
    """Liefert den Operationszähler der bestehenden CDX (Header 0x08, BE), sonst *default*."""
    try:
        with open(cdx_path, "rb") as f:
            head = f.read(12)
        if len(head) >= 12:
            return struct.unpack_from(">I", head, 8)[0]
    except OSError:
        pass
    return default


def write_cdx(filepath: str) -> str | None:
    """Rebuild ``<table>.CDX`` in place from *filepath*'s current contents.

    Supports the single-tag schema used by every Schichtplaner5 table
    (``ID_TAG``) and ``-L`` journal (``NUMBER_TAG``). The tag name, key
    expression and key-op counter are read back from the existing CDX so the
    rebuild always matches whatever tag the original created. Returns the
    written CDX path, or ``None`` if there is no CDX / the key expression could
    not be determined (the caller then falls back to deleting the stale CDX).
    Raises ``ValueError`` for an unsupported key shape.
    """
    base, _ = os.path.splitext(filepath)
    cdx_path = base + ".CDX"
    if not os.path.exists(cdx_path):
        # Kleingeschriebene Variante versuchen; gibt es gar keine CDX, erwartet
        # das Original auch keine Pflege — „keine CDX" signalisieren.
        if os.path.exists(base + ".cdx"):
            cdx_path = base + ".cdx"
        else:
            return None

    key_expr = _read_key_expr(cdx_path)
    if key_expr is None:
        return None
    tag_name = _read_tag_name(cdx_path) or _TAG_NAME

    num_records = len(read_dbf(filepath))
    counter = _read_counter(cdx_path, default=num_records)
    data = build_cdx_bytes(filepath, key_expr, counter, tag_name)

    tmp = cdx_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, cdx_path)
    return cdx_path


def _read_tag_name(cdx_path: str) -> str | None:
    """Liest den Tag-Namen aus dem Tag-Listen-Blatt der bestehenden CDX."""
    try:
        with open(cdx_path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if len(data) < (_TAGLIST_ROOT_PAGE + 1) * PAGE:
        return None
    taglist_root = struct.unpack_from("<I", data, 0)[0]
    leaf = data[taglist_root : taglist_root + PAGE]
    if len(leaf) < PAGE or struct.unpack_from("<H", leaf, 2)[0] < 1:
        return None
    recno_bits = leaf[20]
    dup_mask = leaf[18]
    trail_mask = leaf[19]
    dup_bits = leaf[21]
    entry_bytes = leaf[23] or 3
    val = int.from_bytes(leaf[24 : 24 + entry_bytes], "little")
    dup = (val >> recno_bits) & dup_mask
    trail = (val >> (recno_bits + dup_bits)) & trail_mask
    fresh = _TAGLIST_KEYLEN - dup - trail
    name = leaf[PAGE - fresh : PAGE].rstrip(b"\x00\x20")
    return name.decode("ascii", errors="replace") or None


def _read_key_expr(cdx_path: str) -> str | None:
    """Liest den ``ID_TAG``-Schlüsselausdruck aus dem Expr-Pool der bestehenden CDX.

    Der Ausdruckstext ist die verlässlichste Quelle der realen Schlüsselform
    (das Original kann einen nicht katalogisierten Schlüssel nutzen); ihn
    zurückzulesen heißt, der Neuaufbau passt immer zu dem Tag, den das
    Original angelegt hat.
    """
    try:
        with open(cdx_path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if len(data) < _TAGLIST_ROOT_PAGE * PAGE + PAGE:
        return None
    # Tag-Listen-Blatt → Tag-Header-Offset → Schlüssellänge; der Expression-
    # Pool liegt direkt nach dem Tag-Header. Im Ein-Tag-SP5-Schema ist der
    # Tag-Header Seite 2 und sein Expression-Pool Seite 3.
    taglist_root = struct.unpack_from("<I", data, 0)[0]
    leaf = data[taglist_root : taglist_root + PAGE]
    if len(leaf) < PAGE:
        return None
    recno_mask = struct.unpack_from("<I", leaf, 14)[0]
    entry_bytes = leaf[23] or 3
    if struct.unpack_from("<H", leaf, 2)[0] < 1:
        return None
    tag_hdr_off = int.from_bytes(leaf[24 : 24 + entry_bytes], "little") & recno_mask
    expr_off = tag_hdr_off + PAGE  # expression pool page follows the tag header
    blob = data[expr_off : expr_off + PAGE]
    key_expr = blob.split(b"\x00", 1)[0].decode("ascii", errors="replace")
    return key_expr or None
