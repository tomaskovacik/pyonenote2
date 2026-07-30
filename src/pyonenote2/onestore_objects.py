"""
Real object-tree resolution for FSSHTTPB-encoded .one files.

Builds on fsshttpb.py's Data Element Package parser to:
  1. Follow the [MS-ONESTORE] 3.5 reading algorithm (Storage Manifest -> Data
     root -> Storage Index cell/revision mapping -> Cell Manifest ->
     Revision Manifest chain) to get the full, current set of objects for
     any cell (context, object_space_id) - generalized so it works for the
     top-level "Data root" cell as well as each page's own cell.
  2. Decode each object's ObjectSpaceObjectPropSet (MS-ONESTORE 2.6.1):
     OIDs/OSIDs/ContextIDs CompactID streams + body PropertySet.
  3. Resolve CompactID references using the [MS-ONESTORE] 2.7.8 Mapping
     Table. The spec text alone is ambiguous about *how* the table is
     used; cross-checked against the msiemens/onenote.rs Rust
     implementation (onestore/fsshttpb/object.rs, mapping_table.rs) and
     confirmed: OID-stream CompactIDs are matched *positionally* against
     the Object Data's own Extended GUID Array (not via
     CompactId.guidIndex as an array index - that was an earlier,
     incorrect assumption that happened to work by coincidence in simple
     cases with no Cell ID Array entries). OSID/ContextID-stream
     CompactIDs are matched positionally against the Cell ID Array,
     split into "same object space" (-> ContextID, keep EXGUID1) vs
     "different object space" (-> ObjectSpaceID, keep the whole CellId)
     buckets based on whether the Cell ID's EXGUID2 equals the *current*
     object space id. Ambiguous cases (same CompactId value appearing
     more than once) fall back to a table keyed by the raw CompactId
     value with a list of (position, target) pairs, matching
     onenote.rs's MappingTable::get exactly.
  4. Recursively walk from the section's content root through
     ElementChildNodes / ChildGraphSpaceElementNodes to recover the real
     page tree and real per-page content, instead of guessing page
     membership from byte-offset proximity.
"""
import struct
import uuid

from . import fsshttpb as fb

ZERO_EXGUID = (uuid.UUID(int=0), 0)
DEFAULT_CONTEXT = (uuid.UUID('84DEFAB9-AAA3-4A0D-A3A8-520C77AC7073'), 1)


class Cell:
    """A resolved object space / cell: its current, fully-merged object map
    (keyed by (object_guid, partition_id)) plus its root declarations."""

    def __init__(self, object_space_id, object_map, roots):
        self.object_space_id = object_space_id
        self.object_map = object_map
        self.roots = roots

    def content_root(self):
        return self.roots.get(1)

    def metadata_root(self):
        return self.roots.get(2)


def _walk_revision_chain(elements, revision_manifest_el):
    """Newest-to-oldest walk (matching onenote.rs's Revision::parse):
    first-seen (i.e. newest) object/root wins."""
    rev_by_id = {e['revision_manifest']['revision_id']: e['revision_manifest']
                 for e in elements if e['type'] == 0x04}
    og_by_guid = {e['extended_guid']: e['object_group'] for e in elements if e['type'] == 0x05}

    roots = {}
    object_map = {}
    cur = revision_manifest_el
    while cur is not None:
        for r in cur['roots']:
            roots.setdefault(r['root_extended_guid'][1], r['object_extended_guid'])
        for ogref in cur['object_group_references']:
            og = og_by_guid.get(ogref)
            if og is None:
                continue
            for (_, decl), (_, item) in zip(og['declarations'], og['data']):
                object_map.setdefault((decl['object_extended_guid'], decl['partition_id']), (decl, item))
        base = cur['base_revision_id']
        cur = rev_by_id.get(base) if base != ZERO_EXGUID else None
    return object_map, roots


def resolve_cell(elements, storage_index, cell_id):
    """Generalized version of the [MS-ONESTORE] 3.5 algorithm (steps 8-13)
    for an arbitrary Cell ID, not just the Data root."""
    cell_mapping_guid = None
    for cid, cmeg, _serial in storage_index['storage_index']['cell_mappings']:
        if cid == cell_id:
            cell_mapping_guid = cmeg
            break
    if cell_mapping_guid is None:
        return None

    cell_manifest_el = next((e for e in elements if e['type'] == 0x03
                              and e['extended_guid'] == cell_mapping_guid), None)
    if cell_manifest_el is None:
        return None
    current_rev = cell_manifest_el['cell_manifest']['current_revision']

    rev_mapping_guid = None
    for reg, rmeg, _serial in storage_index['storage_index']['revision_mappings']:
        if reg == current_rev:
            rev_mapping_guid = rmeg
            break
    # fallback per onenote.rs: try using current_rev itself as a revision mapping key
    lookup_guid = rev_mapping_guid if rev_mapping_guid is not None else current_rev
    rev_manifest_el = next((e for e in elements if e['type'] == 0x04
                             and e['extended_guid'] == lookup_guid), None)
    if rev_manifest_el is None:
        return None

    object_map, roots = _walk_revision_chain(elements, rev_manifest_el['revision_manifest'])
    return Cell(cell_id[1], object_map, roots)


def get_data_root_cell(elements):
    storage_manifest = next(e for e in elements if e['type'] == 0x02)
    storage_index = next(e for e in elements if e['type'] == 0x01)
    data_root = next(r for r in storage_manifest['storage_manifest']['roots']
                      if r['root_extended_guid'][1] == 2)
    return resolve_cell(elements, storage_index, data_root['cell_id'])


def _stream(blob, pos):
    """ObjectSpaceObjectStreamOfOIDs/OSIDs/ContextIDs: 4-byte header + CompactIDs."""
    hdr = struct.unpack_from('<I', blob, pos)[0]
    count = hdr & 0xFFFFFF
    ext_present = bool((hdr >> 30) & 1)
    not_present = bool((hdr >> 31) & 1)
    pos += 4
    cids = [blob[pos + i * 4:pos + i * 4 + 4] for i in range(count)]
    pos += count * 4
    return cids, ext_present, not_present, pos


def parse_object_space_object_propset(blob):
    oid_cids, _ext1, notpresent1, pos = _stream(blob, 0)
    osid_cids = []
    ctxid_cids = []
    if not notpresent1:
        osid_cids, ext2, _np2, pos = _stream(blob, pos)
        if ext2:
            ctxid_cids, _ext3, _np3, pos = _stream(blob, pos)

    cProperties = struct.unpack_from('<H', blob, pos)[0]
    pos += 2
    prids = list(struct.unpack_from('<%dI' % cProperties, blob, pos))
    pos += 4 * cProperties

    oid_i = osid_i = ctx_i = 0
    props = {}
    for prid in prids:
        ptype = (prid >> 26) & 0x1F
        boolval = bool((prid >> 31) & 1)
        if ptype == 0x1:
            props[prid] = None
        elif ptype == 0x2:
            props[prid] = boolval
        elif ptype == 0x3:
            props[prid] = blob[pos]
            pos += 1
        elif ptype == 0x4:
            props[prid] = struct.unpack_from('<H', blob, pos)[0]
            pos += 2
        elif ptype == 0x5:
            props[prid] = struct.unpack_from('<I', blob, pos)[0]
            pos += 4
        elif ptype == 0x6:
            props[prid] = struct.unpack_from('<Q', blob, pos)[0]
            pos += 8
        elif ptype == 0x7:
            cb = struct.unpack_from('<I', blob, pos)[0]
            pos += 4
            props[prid] = blob[pos:pos + cb]
            pos += cb
        elif ptype == 0x8:
            props[prid] = ('oid', oid_cids[oid_i])
            oid_i += 1
        elif ptype == 0x9:
            n = struct.unpack_from('<I', blob, pos)[0]
            pos += 4
            props[prid] = ('oid*', oid_cids[oid_i:oid_i + n])
            oid_i += n
        elif ptype == 0xA:
            props[prid] = ('osid', osid_cids[osid_i])
            osid_i += 1
        elif ptype == 0xB:
            n = struct.unpack_from('<I', blob, pos)[0]
            pos += 4
            props[prid] = ('osid*', osid_cids[osid_i:osid_i + n])
            osid_i += n
        elif ptype == 0xC:
            props[prid] = ('ctx', ctxid_cids[ctx_i])
            ctx_i += 1
        elif ptype == 0xD:
            n = struct.unpack_from('<I', blob, pos)[0]
            pos += 4
            props[prid] = ('ctx*', ctxid_cids[ctx_i:ctx_i + n])
            ctx_i += n
        else:
            raise NotImplementedError('property type %#x not implemented (prid %#x)' % (ptype, prid))
    return props, oid_cids, osid_cids, ctxid_cids


def _build_lookup(compact_ids, targets):
    """Positional zip -> dict keyed by raw CompactId bytes -> list of
    (position, target), matching onenote.rs's MappingTable::from_entries."""
    table = {}
    for i, (cid, target) in enumerate(zip(compact_ids, targets)):
        table.setdefault(cid, []).append((i, target))
    return table


def _lookup(table, index, cid):
    entries = table.get(cid)
    if not entries:
        return None
    if len(entries) == 1:
        return entries[0][1]
    for i, target in entries:
        if i == index:
            return target
    return None


def decode_object(cell, key):
    """key = (object_guid, 1). Returns dict prid -> resolved value.
    Scalars/bytes pass through. ('oid',cid)/('osid',cid)/('ctx',cid) single
    references resolve to an ExGuid (oid/ctx) or CellId (osid) tuple, or
    None if unresolvable. Array variants resolve to a list of same."""
    decl, item = cell.object_map[key]
    props, oid_cids, osid_cids, ctxid_cids = parse_object_space_object_propset(item['data'])

    object_refs = item.get('object_extended_guids', [])
    cell_ids = item.get('cell_ids', [])
    object_space_id = cell.object_space_id

    context_refs = [c1 for (c1, c2) in cell_ids if c2 == object_space_id]
    object_space_refs = [(c1, c2) for (c1, c2) in cell_ids if c2 != object_space_id]

    oid_table = _build_lookup(oid_cids, object_refs)
    ctx_table = _build_lookup(ctxid_cids, context_refs)
    osid_table = _build_lookup(osid_cids, object_space_refs)
    # OID + ContextID share one combined "objects" mapping table in
    # onenote.rs (chained iterators over the same dict); merge them here.
    obj_table = dict(oid_table)
    for cid, entries in ctx_table.items():
        obj_table.setdefault(cid, []).extend(entries)

    def resolve_single(kind, cid):
        idx = (oid_cids if kind == 'oid' else ctxid_cids if kind == 'ctx' else osid_cids).index(cid)
        table = osid_table if kind == 'osid' else obj_table
        return _lookup(table, idx, cid)

    resolved = {}
    for prid, val in props.items():
        if val is None or not isinstance(val, tuple) or val[0] not in ('oid', 'oid*', 'osid', 'osid*', 'ctx', 'ctx*'):
            resolved[prid] = val
            continue
        kind, payload = val
        if kind in ('oid', 'osid', 'ctx'):
            resolved[prid] = resolve_single(kind, payload)
        else:
            base_kind = kind[:-1]
            resolved[prid] = [resolve_single(base_kind, c) for c in payload]
    return resolved
