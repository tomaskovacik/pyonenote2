"""
[MS-FSSHTTPB] (File Synchronization via SOAP over HTTP, Binary) primitive
decoders.

Modern .one/.onetoc2 files synced through OneDrive/SharePoint embed a real
FSSHTTPB Data Element Package rather than a classic flat MS-ONESTORE
FileNodeList container. This module implements just enough of MS-FSSHTTPB
to walk that package: Compact Unsigned 64-bit Integer, Extended GUID (all
size variants), Serial Number, Cell ID, Stream Object Headers (16/32-bit
start, 8/16-bit end), and the Data Element Package itself (Storage Index,
Storage Manifest, Cell Manifest, Revision Manifest, Object Group, Object
Data BLOB).

All bit-packed fields use the convention documented throughout MS-ONESTORE/
MS-FSSHTTPB: values are stored little-endian, and fields are unpacked
starting from the least-significant bit in the order listed by the spec
(first-listed field = lowest bits). Every structure here was verified
byte-exact against real .one files before being trusted (see the project
README for how CompactID resolution specifically was cross-checked against
the msiemens/onenote.rs Rust implementation).
"""
import struct
import uuid


def read_guid(data, offset):
    d1, d2, d3 = struct.unpack_from('<IHH', data, offset)
    d4 = data[offset + 8:offset + 16]
    return uuid.UUID(fields=(d1, d2, d3, d4[0], d4[1], int.from_bytes(d4[2:8], 'big')))


def guid_bytes(guid_str):
    return uuid.UUID(guid_str).bytes_le


# ---------------------------------------------------------------------
# 2.2.1.1 Compact Unsigned 64-bit Integer
# ---------------------------------------------------------------------

def compact_uint(data, offset):
    b0 = data[offset]
    if b0 == 0:
        return 0, offset + 1
    k = (b0 & -b0).bit_length() - 1  # index of lowest set bit, 0..7
    if k < 7:
        width = k + 1
        v = int.from_bytes(data[offset:offset + width], 'little')
        return v >> width, offset + width
    else:
        if b0 != 0x80:
            raise ValueError('invalid compact uint 64-bit marker byte %#x' % b0)
        v = int.from_bytes(data[offset + 1:offset + 9], 'little')
        return v, offset + 9


# ---------------------------------------------------------------------
# 2.2.1.5 Stream Object Header (16-bit / 32-bit start, 8-bit / 16-bit end)
# ---------------------------------------------------------------------

def stream_object_header_16(data, offset):
    v = struct.unpack_from('<H', data, offset)[0]
    header_type = v & 0x3
    if header_type != 0x0:
        raise ValueError('not a 16-bit Stream Object Header Start at %d' % offset)
    compound = bool((v >> 2) & 0x1)
    type_ = (v >> 3) & 0x3F
    length = (v >> 9) & 0x7F
    return dict(compound=compound, type=type_, length=length), offset + 2


def stream_object_header_32(data, offset):
    v = struct.unpack_from('<I', data, offset)[0]
    header_type = v & 0x3
    if header_type != 0x2:
        raise ValueError('not a 32-bit Stream Object Header Start at %d' % offset)
    compound = bool((v >> 2) & 0x1)
    type_ = (v >> 3) & 0x3FFF
    length = (v >> 17) & 0x7FFF
    pos = offset + 4
    if length == 32767:
        length, pos = compact_uint(data, pos)
    return dict(compound=compound, type=type_, length=length), pos


def stream_object_header_end_8(data, offset):
    v = data[offset]
    header_type = v & 0x3
    if header_type != 0x1:
        raise ValueError('not an 8-bit Stream Object Header End at %d' % offset)
    type_ = (v >> 2) & 0x3F
    return dict(type=type_), offset + 1


def stream_object_header_end_16(data, offset):
    v = struct.unpack_from('<H', data, offset)[0]
    header_type = v & 0x3
    if header_type != 0x3:
        raise ValueError('not a 16-bit Stream Object Header End at %d' % offset)
    type_ = (v >> 2) & 0x3FFF
    return dict(type=type_), offset + 2


# ---------------------------------------------------------------------
# 2.2.1.7 Extended GUID (variable width; same lowest-set-bit trick as
# Compact Unsigned 64-bit Integer, but with type markers 4/32/64/128
# rather than 1/2/4/8/16/32/64/128, and a trailing 16-byte GUID).
# ---------------------------------------------------------------------

def extended_guid(data, offset):
    b0 = data[offset]
    if b0 == 0:
        return (uuid.UUID(int=0), 0), offset + 1
    k = (b0 & -b0).bit_length() - 1
    if k == 2:  # type=4, 5-bit value, 17 bytes total
        value = b0 >> 3
        g = read_guid(data, offset + 1)
        return (g, value), offset + 17
    elif k == 5:  # type=32, 10-bit value, 18 bytes total
        v16 = struct.unpack_from('<H', data, offset)[0]
        value = v16 >> 6
        g = read_guid(data, offset + 2)
        return (g, value), offset + 18
    elif k == 6:  # type=64, 17-bit value, 19 bytes total
        v24 = int.from_bytes(data[offset:offset + 3], 'little')
        value = v24 >> 7
        g = read_guid(data, offset + 3)
        return (g, value), offset + 19
    elif b0 == 0x80:  # type=128, 32-bit value, 21 bytes total
        value = struct.unpack_from('<I', data, offset + 1)[0]
        g = read_guid(data, offset + 5)
        return (g, value), offset + 21
    raise ValueError('invalid Extended GUID marker byte %#x at %d' % (b0, offset))


# ---------------------------------------------------------------------
# 2.2.1.9 Serial Number
# ---------------------------------------------------------------------

def serial_number(data, offset):
    b0 = data[offset]
    if b0 == 0:
        return None, offset + 1
    if b0 != 0x80:
        raise ValueError('invalid Serial Number marker byte %#x at %d' % (b0, offset))
    g = read_guid(data, offset + 1)
    value = struct.unpack_from('<Q', data, offset + 17)[0]
    return (g, value), offset + 25


# ---------------------------------------------------------------------
# 2.2.1.10 Cell ID = two Extended GUIDs
# ---------------------------------------------------------------------

def cell_id(data, offset):
    exguid1, offset = extended_guid(data, offset)
    exguid2, offset = extended_guid(data, offset)
    return (exguid1, exguid2), offset


STORAGE_MANIFEST_SCHEMA_GUID_ONE = '1F937CB4-B26F-445F-B9F8-17E20160E461'
STORAGE_MANIFEST_SCHEMA_GUID_TOC2 = 'E4DBFD38-E5C7-408B-A8A1-0E7B421E1F5F'
HEADER_CELL_ROOT_GUID = '1A5A319C-C26B-41AA-B9C5-9BD8C44E07D4'

DATA_ELEMENT_TYPES = {
    0x01: 'Storage Index',
    0x02: 'Storage Manifest',
    0x03: 'Cell Manifest',
    0x04: 'Revision Manifest',
    0x05: 'Object Group',
    0x06: 'Data Element Fragment',
    0x0A: 'Object Data BLOB',
}

STREAM_OBJECT_TYPES_16 = {
    0x01: 'Data Element', 0x02: 'Object Data BLOB',
    0x03: 'Object Group Object Excluded Data', 0x04: 'Waterline Knowledge Entry',
    0x05: 'Object Group Object Data BLOB Declaration', 0x06: 'Data Element Hash',
    0x07: 'Storage Manifest root declare', 0x0A: 'Revision Manifest root declare',
    0x0B: 'Cell Manifest current revision', 0x0C: 'Storage Manifest schema GUID',
    0x0D: 'Storage Index Revision Mapping', 0x0E: 'Storage Index Cell Mapping',
    0x0F: 'Cell Knowledge Range', 0x10: 'Knowledge',
    0x11: 'Storage Index Manifest Mapping', 0x14: 'Cell Knowledge',
    0x15: 'Data Element Package', 0x16: 'Object Group Object Data',
    0x17: 'Cell Knowledge Entry', 0x18: 'Object Group Object Declare',
    0x19: 'Revision Manifest Object Group references', 0x1A: 'Revision Manifest',
    0x1C: 'Object Group Object Data BLOB reference', 0x1D: 'Object Group Declarations',
    0x1E: 'Object Group Data', 0x29: 'Waterline Knowledge',
    0x2D: 'Content Tag Knowledge', 0x2E: 'Content Tag Knowledge Entry',
    0x30: 'Query Changes Versioning',
}


# ---------------------------------------------------------------------
# Generic Stream Object Header dispatch (16-bit vs 32-bit start; 8-bit vs
# 16-bit end), selected by the 2-bit Header Type in the first byte.
# ---------------------------------------------------------------------

def read_any_soh_start(data, offset):
    header_type = data[offset] & 0x3
    if header_type == 0x0:
        return stream_object_header_16(data, offset)
    if header_type == 0x2:
        return stream_object_header_32(data, offset)
    raise ValueError('not a start SOH at %d (byte %#x)' % (offset, data[offset]))


def read_any_soh_end(data, offset):
    header_type = data[offset] & 0x3
    if header_type == 0x1:
        return stream_object_header_end_8(data, offset)
    if header_type == 0x3:
        return stream_object_header_end_16(data, offset)
    raise ValueError('not an end SOH at %d (byte %#x)' % (offset, data[offset]))


# ---------------------------------------------------------------------
# 2.2.1.8 Extended GUID Array, 2.2.1.11 Cell ID Array, 2.2.1.3 Binary Item
# ---------------------------------------------------------------------

def extended_guid_array(data, offset):
    count, offset = compact_uint(data, offset)
    items = []
    for _ in range(count):
        item, offset = extended_guid(data, offset)
        items.append(item)
    return items, offset


def cell_id_array(data, offset):
    count, offset = compact_uint(data, offset)
    items = []
    for _ in range(count):
        item, offset = cell_id(data, offset)
        items.append(item)
    return items, offset


def binary_item(data, offset):
    length, offset = compact_uint(data, offset)
    content = data[offset:offset + length]
    return content, offset + length


# ---------------------------------------------------------------------
# 2.2.1.12.2 Storage Index Data Element
# ---------------------------------------------------------------------

def parse_storage_index(data, offset):
    manifest_mappings = []
    cell_mappings = []
    revision_mappings = []
    while data[offset] & 0x3 != 0x1:  # until Data Element End (8-bit SOH)
        hdr, pos = stream_object_header_16(data, offset)
        if hdr['type'] == 0x11:  # Storage Index Manifest Mapping
            mmeg, pos = extended_guid(data, pos)
            serial, pos = serial_number(data, pos)
            manifest_mappings.append((mmeg, serial))
        elif hdr['type'] == 0x0E:  # Storage Index Cell Mapping
            cid, pos = cell_id(data, pos)
            cmeg, pos = extended_guid(data, pos)
            serial, pos = serial_number(data, pos)
            cell_mappings.append((cid, cmeg, serial))
        elif hdr['type'] == 0x0D:  # Storage Index Revision Mapping
            reg, pos = extended_guid(data, pos)
            rmeg, pos = extended_guid(data, pos)
            serial, pos = serial_number(data, pos)
            revision_mappings.append((reg, rmeg, serial))
        else:
            raise ValueError('unexpected SOH type %d in storage index at %d' % (hdr['type'], offset))
        offset = pos
    return dict(manifest_mappings=manifest_mappings, cell_mappings=cell_mappings,
                revision_mappings=revision_mappings), offset


# ---------------------------------------------------------------------
# 2.2.1.12.3 Storage Manifest Data Element
# ---------------------------------------------------------------------

def parse_storage_manifest(data, offset):
    hdr, offset = stream_object_header_16(data, offset)
    if hdr['type'] != 0x0C:
        raise ValueError('expected Storage Manifest schema GUID SOH at %d' % offset)
    schema_guid = read_guid(data, offset)
    offset += 16
    roots = []
    while data[offset] & 0x3 != 0x1:
        hdr2, pos = stream_object_header_16(data, offset)
        if hdr2['type'] != 0x07:
            raise ValueError('expected Storage Manifest root declare at %d' % offset)
        root_guid, pos = extended_guid(data, pos)
        cid, pos = cell_id(data, pos)
        roots.append(dict(root_extended_guid=root_guid, cell_id=cid))
        offset = pos
    return dict(schema_guid=schema_guid, roots=roots), offset


# ---------------------------------------------------------------------
# 2.2.1.12.4 Cell Manifest Data Element
# ---------------------------------------------------------------------

def parse_cell_manifest(data, offset):
    hdr, offset = stream_object_header_16(data, offset)
    if hdr['type'] != 0x0B:
        raise ValueError('expected Cell Manifest current revision SOH at %d' % offset)
    current_revision, offset = extended_guid(data, offset)
    return dict(current_revision=current_revision), offset


# ---------------------------------------------------------------------
# 2.2.1.12.5 Revision Manifest Data Element
# ---------------------------------------------------------------------

def parse_revision_manifest(data, offset):
    hdr, offset = stream_object_header_16(data, offset)
    if hdr['type'] != 0x1A:
        raise ValueError('expected Revision Manifest SOH at %d' % offset)
    revision_id, offset = extended_guid(data, offset)
    base_revision_id, offset = extended_guid(data, offset)
    roots = []
    object_group_refs = []
    while data[offset] & 0x3 != 0x1:
        hdr2, pos = stream_object_header_16(data, offset)
        if hdr2['type'] == 0x0A:  # Revision Manifest root declare
            root_guid, pos = extended_guid(data, pos)
            object_guid, pos = extended_guid(data, pos)
            roots.append(dict(root_extended_guid=root_guid, object_extended_guid=object_guid))
        elif hdr2['type'] == 0x19:  # Revision Manifest Object Group references
            og_guid, pos = extended_guid(data, pos)
            object_group_refs.append(og_guid)
        else:
            raise ValueError('unexpected SOH type %d in revision manifest at %d' % (hdr2['type'], offset))
        offset = pos
    return dict(revision_id=revision_id, base_revision_id=base_revision_id,
                roots=roots, object_group_references=object_group_refs), offset


# ---------------------------------------------------------------------
# 2.2.1.12.6 Object Group Data Element
# ---------------------------------------------------------------------

def object_declaration(data, offset):
    hdr, offset = read_any_soh_start(data, offset)
    object_guid, offset = extended_guid(data, offset)
    partition_id, offset = compact_uint(data, offset)
    data_size, offset = compact_uint(data, offset)
    obj_refs, offset = compact_uint(data, offset)
    cell_refs, offset = compact_uint(data, offset)
    return dict(header=hdr, object_extended_guid=object_guid, partition_id=partition_id,
                data_size=data_size, object_references_count=obj_refs,
                cell_references_count=cell_refs), offset


def object_data_blob_declaration(data, offset):
    hdr, offset = read_any_soh_start(data, offset)
    object_guid, offset = extended_guid(data, offset)
    blob_guid, offset = extended_guid(data, offset)
    partition_id, offset = compact_uint(data, offset)
    obj_refs, offset = compact_uint(data, offset)
    cell_refs, offset = compact_uint(data, offset)
    return dict(header=hdr, object_extended_guid=object_guid, blob_extended_guid=blob_guid,
                partition_id=partition_id, object_references_count=obj_refs,
                cell_references_count=cell_refs), offset


def object_data(data, offset):
    hdr, offset = read_any_soh_start(data, offset)
    ext_guids, offset = extended_guid_array(data, offset)
    cids, offset = cell_id_array(data, offset)
    if hdr['type'] == 0x16:  # Object Group Object Data
        content, offset = binary_item(data, offset)
        return dict(header=hdr, object_extended_guids=ext_guids, cell_ids=cids, data=content), offset
    elif hdr['type'] == 0x03:  # Object Group Object Excluded Data
        size, offset = compact_uint(data, offset)
        return dict(header=hdr, object_extended_guids=ext_guids, cell_ids=cids, data_size=size), offset
    raise ValueError('unexpected object data SOH type %d' % hdr['type'])


def object_data_blob_reference(data, offset):
    hdr, offset = read_any_soh_start(data, offset)
    ext_guids, offset = extended_guid_array(data, offset)
    cids, offset = cell_id_array(data, offset)
    blob_guid, offset = extended_guid(data, offset)
    return dict(header=hdr, object_extended_guids=ext_guids, cell_ids=cids,
                blob_extended_guid=blob_guid), offset


def parse_object_group_body(data, offset):
    hdr, pos = read_any_soh_start(data, offset)
    if hdr['type'] != 0x1D:
        raise NotImplementedError(
            'Object Group Data Element field before Declarations Start did not match '
            '(type=%d at %d) - likely an unimplemented optional Data Element Hash' % (hdr['type'], offset))
    offset = pos

    declarations = []
    while True:
        if data[offset] & 0x3 in (0x1, 0x3):
            end, offset = read_any_soh_end(data, offset)
            if end['type'] != 0x1D:
                raise ValueError('expected Object Group Declarations End at %d' % offset)
            break
        dhdr, _ = read_any_soh_start(data, offset)
        if dhdr['type'] == 0x18:
            decl, offset = object_declaration(data, offset)
            declarations.append(('object', decl))
        elif dhdr['type'] == 0x05:
            decl, offset = object_data_blob_declaration(data, offset)
            declarations.append(('blob', decl))
        else:
            raise ValueError('unexpected declaration SOH type %d at %d' % (dhdr['type'], offset))

    if data[offset] & 0x3 == 0x2:
        peek, _ = stream_object_header_32(data, offset)
        if peek['type'] == 0x79:
            raise NotImplementedError('Object Metadata Declaration present at %d - not implemented' % offset)

    ghdr, offset = read_any_soh_start(data, offset)
    if ghdr['type'] != 0x1E:
        raise ValueError('expected Object Group Data Start at %d' % offset)

    items = []
    while True:
        if data[offset] & 0x3 in (0x1, 0x3):
            end, offset = read_any_soh_end(data, offset)
            if end['type'] != 0x1E:
                raise ValueError('expected Object Group Data End at %d' % offset)
            break
        dhdr, _ = read_any_soh_start(data, offset)
        if dhdr['type'] in (0x16, 0x03):
            od, offset = object_data(data, offset)
            items.append(('data', od))
        elif dhdr['type'] == 0x1C:
            odbr, offset = object_data_blob_reference(data, offset)
            items.append(('blobref', odbr))
        else:
            raise ValueError('unexpected object group data SOH type %d at %d' % (dhdr['type'], offset))

    return dict(declarations=declarations, data=items), offset


# ---------------------------------------------------------------------
# 2.2.1.12.8 Object Data BLOB Data Element (embedded file/attachment data)
# ---------------------------------------------------------------------

def parse_object_data_blob(data, offset):
    hdr, offset = read_any_soh_start(data, offset)
    if hdr['type'] != 0x02:
        raise ValueError('expected Object Data BLOB SOH at %d' % offset)
    content = data[offset:offset + hdr['length']]
    offset += hdr['length']
    return content, offset


# ---------------------------------------------------------------------
# 2.2.1.12 Data Element Package: top-level walker
# ---------------------------------------------------------------------

def parse_data_element(data, offset):
    hdr, offset = stream_object_header_16(data, offset)
    if hdr['type'] != 0x01:
        raise ValueError('expected Data Element Start at %d' % offset)
    ext_guid, offset = extended_guid(data, offset)
    serial, offset = serial_number(data, offset)
    det, offset = compact_uint(data, offset)
    elem = dict(extended_guid=ext_guid, serial_number=serial, type=det,
                type_name=DATA_ELEMENT_TYPES.get(det))
    if det == 0x01:
        elem['storage_index'], offset = parse_storage_index(data, offset)
    elif det == 0x02:
        elem['storage_manifest'], offset = parse_storage_manifest(data, offset)
    elif det == 0x03:
        elem['cell_manifest'], offset = parse_cell_manifest(data, offset)
    elif det == 0x04:
        elem['revision_manifest'], offset = parse_revision_manifest(data, offset)
    elif det == 0x05:
        elem['object_group'], offset = parse_object_group_body(data, offset)
    elif det == 0x0A:
        elem['blob'], offset = parse_object_data_blob(data, offset)
    else:
        raise ValueError('unsupported Data Element Type %d at %d' % (det, offset))
    end, offset = stream_object_header_end_8(data, offset)
    if end['type'] != 0x01:
        raise ValueError('expected Data Element End at %d' % offset)
    return elem, offset


def parse_data_element_package(data, offset):
    hdr, offset = stream_object_header_16(data, offset)
    if hdr['type'] != 0x15:
        raise ValueError('expected Data Element Package Start at %d' % offset)
    reserved = data[offset]
    offset += 1
    if reserved != 0:
        raise ValueError('Data Element Package Reserved byte not zero at %d' % offset)
    elements = []
    while data[offset] & 0x3 != 0x1:
        elem, offset = parse_data_element(data, offset)
        elements.append(elem)
    end, offset = stream_object_header_end_8(data, offset)
    if end['type'] != 0x15:
        raise ValueError('expected Data Element Package End at %d' % offset)
    return elements, offset


# ---------------------------------------------------------------------
# 2.8.1 Packaging Structure (the outer .one/.onetoc2 file wrapper around
# the FSSHTTPB Data Element Package)
# ---------------------------------------------------------------------

def parse_packaging_structure(data):
    guid_file_type = read_guid(data, 0)
    guid_file = read_guid(data, 16)
    guid_legacy_file_version = read_guid(data, 32)
    guid_file_format = read_guid(data, 48)
    offset = 68
    hdr, offset = stream_object_header_32(data, offset)
    if hdr['type'] != 0x7A:
        raise ValueError('expected Packaging Start 32-bit SOH at 68')
    storage_index_ext_guid, offset = extended_guid(data, offset)
    guid_cell_schema_id = read_guid(data, offset)
    offset += 16
    elements, offset = parse_data_element_package(data, offset)
    end, offset = stream_object_header_end_16(data, offset)
    if end['type'] != 0x7A:
        raise ValueError('expected Packaging End at %d' % offset)
    return dict(
        guid_file_type=guid_file_type,
        guid_file=guid_file,
        guid_legacy_file_version=guid_legacy_file_version,
        guid_file_format=guid_file_format,
        storage_index_extended_guid=storage_index_ext_guid,
        guid_cell_schema_id=guid_cell_schema_id,
        data_elements=elements,
    ), offset
