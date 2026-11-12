"""Strict table reading: damaged tables are errors, text follows the code page of the header."""

import struct

import pytest

from sp5export.dbf import DbfError, read_table


def _dbf(rows: list[bytes], code_page: int = 0, count: int | None = None, name_len: int = 10) -> bytes:
    """A dBASE III table with the fields ID (N 4) and NAME (C name_len); rows are raw NAME bytes."""
    fields = [(b"ID", b"N", 4), (b"NAME", b"C", name_len)]
    record_size = 1 + sum(f[2] for f in fields)
    header_size = 32 + 32 * len(fields) + 1
    header = bytearray(32)
    header[0] = 0x03
    struct.pack_into("<IHH", header, 4, len(rows) if count is None else count, header_size, record_size)
    header[29] = code_page
    for name, ftype, length in fields:
        header += name.ljust(11, b"\x00") + ftype + bytes(4) + bytes([length, 0]) + bytes(14)
    header += b"\x0d"
    body = b"".join(b" " + str(i + 1).rjust(4).encode() + raw.ljust(name_len, b" ") for i, raw in enumerate(rows))
    return bytes(header) + body + b"\x1a"


def _write(tmp_path, data: bytes):
    path = tmp_path / "5TEST.DBF"
    path.write_bytes(data)
    return path


@pytest.mark.parametrize("code_page,encoding", [(0x02, "cp850"), (0x03, "cp1252"), (0x01, "cp437")])
def test_text_follows_the_code_page(tmp_path, code_page, encoding):
    rows = [w.encode(encoding) for w in ("Müller", "Größe", "Öde")]
    assert [r["NAME"] for r in read_table(_write(tmp_path, _dbf(rows, code_page)))] == ["Müller", "Größe", "Öde"]


def test_cp850_is_not_read_as_latin1(tmp_path):
    raw = "Müller".encode("cp850")
    assert raw.decode("latin-1") != "Müller"  # the old reader's result
    assert read_table(_write(tmp_path, _dbf([raw], 0x02)))[0]["NAME"] == "Müller"


def test_utf16_text_is_independent_of_the_code_page(tmp_path):
    rows = ["Müller".ljust(10).encode("utf-16-le")]
    assert read_table(_write(tmp_path, _dbf(rows, 0x02, name_len=20)))[0]["NAME"] == "Müller"


@pytest.mark.parametrize("text", ["F", "Ü", "Č"])
def test_one_character_utf16_text_is_independent_of_the_code_page(tmp_path, text):
    rows = [(text + "\0").encode("utf-16-le") + b"    "]
    assert read_table(_write(tmp_path, _dbf(rows, 0x02, name_len=8)))[0]["NAME"] == text


def test_unset_code_page_reads_as_before(tmp_path):
    assert read_table(_write(tmp_path, _dbf(["Müller".encode("latin-1")])))[0]["NAME"] == "Müller"


def test_unknown_code_page_is_an_error(tmp_path):
    with pytest.raises(DbfError, match="Codepage"):
        read_table(_write(tmp_path, _dbf([b"A"], 0x7F)))


def test_deleted_records_are_skipped(tmp_path):
    data = bytearray(_dbf([b"A", b"B", b"C"]))
    data[32 + 64 + 1 + 15] = 0x2A  # deletion flag of the second record
    assert [r["NAME"] for r in read_table(_write(tmp_path, bytes(data)))] == ["A", "C"]


def test_truncated_file_is_an_error(tmp_path):
    data = _dbf([b"A", b"B", b"C"])
    with pytest.raises(DbfError, match="laut Dateikopf 3 Datensätze, vorhanden 1"):
        read_table(_write(tmp_path, data[:-1 - 15 - 7]))


def test_more_records_than_the_header_says_is_an_error(tmp_path):
    with pytest.raises(DbfError, match="enthält mehr"):
        read_table(_write(tmp_path, _dbf([b"A", b"B", b"C"], count=2)))


def test_partial_record_after_the_last_one_is_an_error(tmp_path):
    with pytest.raises(DbfError, match="enthält mehr"):
        read_table(_write(tmp_path, _dbf([b"A", b"B"])[:-1] + b" 3  C"))


def test_file_without_end_mark_is_accepted(tmp_path):
    assert len(read_table(_write(tmp_path, _dbf([b"A", b"B"])[:-1]))) == 2


@pytest.mark.parametrize("data", [b"", b"\x03" + bytes(20), _dbf([b"A"])[:40]], ids=["empty", "short", "header"])
def test_damaged_header_is_an_error(tmp_path, data):
    with pytest.raises(DbfError, match="beschädigt"):
        read_table(_write(tmp_path, data))


def test_unreadable_file_is_an_error(tmp_path):
    (tmp_path / "5TEST.DBF").mkdir()
    with pytest.raises(DbfError, match="nicht lesbar"):
        read_table(tmp_path / "5TEST.DBF")
