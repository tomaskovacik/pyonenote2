"""Unit tests for the low-level [MS-FSSHTTPB] primitive decoders.

These exercise the byte-level encodings directly (no sample .one file
needed) -- several of the exact byte sequences below were validated by
hand against a real .one file's Storage Manifest Data Element before
being turned into these regression tests.
"""
import uuid

from pyonenote2 import fsshttpb as fb


def test_compact_uint_zero():
    assert fb.compact_uint(b"\x00", 0) == (0, 1)


def test_compact_uint_7bit():
    # value=2 -> 1-byte encoding: A=1 | (2<<1) = 0x05
    assert fb.compact_uint(b"\x05", 0) == (2, 1)


def test_compact_uint_64bit_marker():
    data = b"\x80" + (12345).to_bytes(8, "little")
    assert fb.compact_uint(data, 0) == (12345, 9)


def test_extended_guid_null():
    (g, n), pos = fb.extended_guid(b"\x00", 0)
    assert g == uuid.UUID(int=0)
    assert n == 0
    assert pos == 1


def test_extended_guid_5bit_uint():
    # type=4 (0b100), value=1 -> b0 = 4 | (1<<3) = 0x0C
    g = uuid.uuid4()
    data = bytes([0x0C]) + g.bytes_le
    (parsed_guid, n), pos = fb.extended_guid(data, 0)
    assert parsed_guid == g
    assert n == 1
    assert pos == 17


def test_stream_object_header_16_storage_manifest_schema_guid():
    # Verified against real TODO.one bytes: Type=0x0C (Storage Manifest
    # schema GUID), Length=16, Compound=False -> bytes 60 20.
    hdr, pos = fb.stream_object_header_16(b"\x60\x20", 0)
    assert hdr == dict(compound=False, type=0x0C, length=16)
    assert pos == 2


def test_stream_object_header_32_packaging_start():
    # Verified against real TODO.one bytes: Type=0x7A (Packaging Start),
    # Compound=True, Length=33 -> bytes d6 03 42 00.
    hdr, pos = fb.stream_object_header_32(b"\xd6\x03\x42\x00", 0)
    assert hdr == dict(compound=True, type=0x7A, length=33)
    assert pos == 4


def test_cell_id_round_trip():
    g1, g2 = uuid.uuid4(), uuid.uuid4()
    data = bytes([0x0C]) + g1.bytes_le + bytes([0x0C]) + g2.bytes_le
    (exg1, exg2), pos = fb.cell_id(data, 0)
    assert exg1 == (g1, 1)
    assert exg2 == (g2, 1)
    assert pos == 34


def test_read_guid_matches_storage_manifest_schema_guid_one():
    data = fb.guid_bytes(fb.STORAGE_MANIFEST_SCHEMA_GUID_ONE)
    parsed = fb.read_guid(data, 0)
    assert str(parsed).upper() == fb.STORAGE_MANIFEST_SCHEMA_GUID_ONE
