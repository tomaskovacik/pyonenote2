"""Reader/converter for OneNote (.one / .onetoc2) notebook folders.

Primary path: genuine [MS-ONESTORE]/[MS-FSSHTTPB] object-tree resolution
(see onestore_page.py / onestore_objects.py / fsshttpb.py) -- real page
order and real per-page content, recovered by actually walking the file's
object graph rather than guessing from byte-offset proximity.

Fallback path (used automatically per-file if the real path raises, or
returns nothing -- e.g. an older on-disk layout, or a structure this
library doesn't understand yet): a heuristic UTF-16LE text-run scanner.
OneNote always stores paragraph/title/hyperlink text as plain UTF-16LE
runs regardless of container format, so even where the real object graph
can't be walked, scanning for those runs, filtering recurring
style/metadata noise (font names, author/timestamp revision metadata,
GUIDs), and collapsing OneNote's incremental edit-history duplicates (a
paragraph gets re-stored every time it's edited) recovers *something*.
Page boundaries in this fallback are a heuristic guess and indentation
depth isn't recoverable at all -- treat its output as best-effort, not
byte-exact.

`.onetoc2` (the notebook table of contents) is read separately and more
simply: it still uses the classic flat MS-ONESTORE FileNodeList container
(unlike `.one` section files, at least for the notebooks this was
developed against), so section discovery/ordering here is a lightweight
text-run scan for `*.one` filenames rather than a full FileNodeList walk.
"""

from __future__ import annotations

import argparse
import bisect
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

from . import onestore_page

# --- low-level UTF-16LE text-run extraction (heuristic fallback) ----------

# Printable ASCII, Latin-1 Supplement + Latin Extended-A (covers most
# European-language diacritics), plus a few punctuation marks OneNote
# commonly uses (curly quotes, en/em dash).
_EXTRA_CODEPOINTS = {0x2018, 0x2019, 0x201C, 0x201D, 0x2013, 0x2014, 0x2026}


def _is_allowed_codepoint(cp: int) -> bool:
    if 0x20 <= cp <= 0x7E:
        return True
    if 0xA0 <= cp <= 0x17F:
        return True
    return cp in _EXTRA_CODEPOINTS


def extract_runs(data: bytes, min_chars: int = 3) -> list[tuple[int, str]]:
    """Scan raw bytes for contiguous UTF-16LE printable-text runs."""
    runs: list[tuple[int, str]] = []
    i = 0
    n = len(data)
    while i < n - 1:
        j = i
        chars: list[str] = []
        while j < n - 1:
            lo, hi = data[j], data[j + 1]
            cp = lo | (hi << 8)
            if hi == 0 and 0x20 <= lo <= 0x7E:
                chars.append(chr(cp))
                j += 2
            elif _is_allowed_codepoint(cp):
                chars.append(chr(cp))
                j += 2
            else:
                break
        if len(chars) >= min_chars:
            runs.append((i, "".join(chars)))
            i = j
        else:
            # Deliberately +1, not +2: text runs are not guaranteed to sit
            # at even file offsets relative to our scan start (a preceding
            # property/header of odd length shifts them), and skipping by 2
            # would make the scanner permanently out of phase with -- and
            # therefore blind to -- any run at the "wrong" parity.
            i += 1
    return runs


# --- property-based extraction (per the [MS-ONE] spec) --------------------
#
# Individual property records are directly findable even without walking
# the full object graph: Microsoft's public [MS-ONE] spec defines fixed
# 32-bit PropertyID constants (e.g. CachedTitleString = 0x1C001CF3,
# SectionDisplayName = 0x1C00349B), and searching for those literal 4-byte
# values in the raw file finds real property records at plausible
# frequencies.
#
# The value encoding used below: a PropertyID is followed, within a short
# window, by the byte sequence 0x10000000 (a 16-byte length marker) + a
# 16-byte context GUID + a 4-byte length + that many bytes of
# null-terminated UTF-16LE text (a WzInAtom, per [MS-ONE] section 2.2.9).

_PROPERTY_ID_CACHED_TITLE_STRING = 0x1C001CF3
_PROPERTY_ID_SECTION_DISPLAY_NAME = 0x1C00349B

_WZ_STRING_LENGTH_MARKER = b"\x10\x00\x00\x00"
_WZ_STRING_SEARCH_WINDOW = 200
_WZ_STRING_MAX_LEN = 1000


def _decode_wz_string_after(data: bytes, pid_offset: int) -> str | None:
    """Decode the WzInAtom string value following a PropertyID occurrence
    at `pid_offset` (see section comment above for the byte layout)."""
    region = data[pid_offset:pid_offset + _WZ_STRING_SEARCH_WINDOW]
    marker_idx = region.find(_WZ_STRING_LENGTH_MARKER)
    if marker_idx == -1:
        return None

    guid_start = pid_offset + marker_idx + len(_WZ_STRING_LENGTH_MARKER)
    length_off = guid_start + 16
    if length_off + 4 > len(data):
        return None

    length = struct.unpack_from("<I", data, length_off)[0]
    if length <= 0 or length > _WZ_STRING_MAX_LEN or length % 2 != 0:
        return None

    text_off = length_off + 4
    if text_off + length > len(data):
        return None

    try:
        text = data[text_off:text_off + length].decode("utf-16-le")
    except UnicodeDecodeError:
        return None

    return text.rstrip("\x00").strip()


def extract_property_strings(data: bytes, property_id: int
                              ) -> list[tuple[int, str]]:
    """Find every occurrence of `property_id` and decode its string value.

    Returns (offset, value) pairs in file order. Multiple entries with the
    same value are expected (OneNote re-stores a property on every
    revision) -- callers that want a deduplicated view should track that
    themselves, same as with the rest of this module's raw-run outputs.
    """
    pid_bytes = struct.pack("<I", property_id)
    results: list[tuple[int, str]] = []
    idx = 0
    while True:
        idx = data.find(pid_bytes, idx)
        if idx == -1:
            break
        value = _decode_wz_string_after(data, idx)
        if value:
            results.append((idx, value))
        idx += 1
    return results


# --- paragraph text extraction (real PropertySet parsing) ------------------
#
# RichEditTextUnicode (a paragraph's actual text) doesn't decode with the
# simple heuristic above -- a paragraph's PropertySet carries several
# properties together (TextRunFormatting, ParagraphStyle, ...), and where a
# given property's *value* lives depends on every other property declared
# in the same set, per [MS-ONESTORE] section 2.6.7 PropertySet:
#
#   cProperties (2 bytes) + rgPrids (cProperties x 4-byte PropertyID) +
#   rgData (concatenated values, in rgPrids order, for the properties
#   whose PropertyID.type actually stores data in rgData)
#
# PropertyID.type (top 6 bits of the 4-byte value) controls both meaning
# and rgData width, per [MS-ONESTORE] section 2.6.6 PropertyID:
#
#   1 NoData                        0 bytes
#   2 Bool                          0 bytes (value is the PropertyID's own
#                                    boolValue bit, not stored in rgData)
#   3/4/5/6 One/Two/Four/EightBytesOfData   1/2/4/8 bytes, fixed
#   7 FourBytesOfLengthFollowedByData       4-byte length + that many bytes
#     (what RichEditTextUnicode is)
#   8/10/12 ObjectID/ObjectSpaceID/ContextID   0 bytes -- the actual
#     CompactID lives in a separate OIDs/OSIDs/ContextIDs stream entirely
#     (see [MS-ONESTORE] 2.6.1 ObjectSpaceObjectPropSet)
#   9/11/13 ArrayOf{ObjectIDs,ObjectSpaceIDs,ContextIDs}   4 bytes -- just
#     the element count; the array itself is in the same separate stream
#     as its singular counterpart above
#   16 ArrayOfPropertyValues          prtArrayOfPropertyValues (2.6.9)
#   17 PropertySet                    a single nested PropertySet inline
#
# So finding a property's value means locating the PropertySet's real
# start (search backward for a cProperties count whose rgPrids array both
# validates and contains our target PropertyID), then walking rgPrids up
# to that point summing each preceding property's rgData width via the
# type rules above -- recursively, for the nested-PropertySet cases.

_VALID_PROPERTY_TYPES = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 16, 17}
_FIXED_RGDATA_WIDTH = {
    1: 0, 2: 0, 3: 1, 4: 2, 5: 4, 6: 8,
    8: 0, 10: 0, 12: 0,
    9: 4, 11: 4, 13: 4,
}  # None/absent from this table = needs type-specific logic (7, 16, 17)
_PROPERTY_ID_RICH_EDIT_TEXT_UNICODE = 0x1C001C22
_PROPERTYSET_SEARCH_BACK = 120
_MAX_PROPERTIES_IN_SET = 40
_MAX_PROPERTYSET_RECURSION = 6


def _read_property_ids(data: bytes, arr_off: int, cprops: int) -> list[int] | None:
    arr_end = arr_off + 4 * cprops
    if arr_end > len(data):
        return None
    ids = []
    for i in range(cprops):
        val = struct.unpack_from("<I", data, arr_off + 4 * i)[0]
        if (val >> 26) not in _VALID_PROPERTY_TYPES:
            return None
        ids.append(val)
    return ids


def _propertyset_width(data: bytes, offset: int, depth: int = 0) -> int | None:
    """Total byte width (cProperties + rgPrids + rgData) of the PropertySet
    starting at `offset`, or None if it doesn't parse as one. Used both to
    skip a nested PropertySet (type 17) and each element of a
    ArrayOfPropertyValues (type 16, whose elements are always type-17)."""
    if depth > _MAX_PROPERTYSET_RECURSION or offset + 2 > len(data):
        return None
    cprops = struct.unpack_from("<H", data, offset)[0]
    if cprops > _MAX_PROPERTIES_IN_SET:
        return None
    ids = _read_property_ids(data, offset + 2, cprops)
    if ids is None:
        return None
    pos = offset + 2 + 4 * cprops
    for val in ids:
        width = _property_value_width(data, pos, val >> 26, depth)
        if width is None:
            return None
        pos += width
    return pos - offset


def _property_value_width(data: bytes, pos: int, prop_type: int,
                           depth: int = 0) -> int | None:
    """rgData bytes consumed by one property of `prop_type` starting at
    `pos`. None means unknown (ran past EOF, malformed nested structure)."""
    fixed = _FIXED_RGDATA_WIDTH.get(prop_type)
    if fixed is not None:
        return fixed if pos + fixed <= len(data) else None

    if prop_type == 7:
        if pos + 4 > len(data):
            return None
        cb = struct.unpack_from("<I", data, pos)[0]
        return 4 + cb if pos + 4 + cb <= len(data) else None

    if prop_type == 16:
        if pos + 4 > len(data):
            return None
        count = struct.unpack_from("<I", data, pos)[0]
        if count == 0:
            return 4
        if pos + 8 > len(data):
            return None
        prid = struct.unpack_from("<I", data, pos + 4)[0]
        if (prid >> 26) != 17:
            return None  # spec: prid.type MUST be PropertySet for arrays
        total = 8
        elem_pos = pos + 8
        for _ in range(count):
            w = _propertyset_width(data, elem_pos, depth + 1)
            if w is None:
                return None
            total += w
            elem_pos += w
        return total

    if prop_type == 17:
        return _propertyset_width(data, pos, depth + 1)

    return None


def _find_propertyset_start(data: bytes, pid_offset: int):
    """Search backward for the real cProperties+rgPrids framing that
    contains the PropertyID at pid_offset. Returns
    (set_start, property_ids, rgdata_start) or None."""
    earliest = max(0, pid_offset - _PROPERTYSET_SEARCH_BACK)
    for start in range(pid_offset, earliest - 1, -1):
        if start + 2 > len(data):
            continue
        cprops = struct.unpack_from("<H", data, start)[0]
        if not (0 < cprops <= _MAX_PROPERTIES_IN_SET):
            continue

        arr_off = start + 2
        arr_end = arr_off + 4 * cprops
        if arr_end > len(data) or not (arr_off <= pid_offset < arr_end):
            continue
        if (pid_offset - arr_off) % 4 != 0:
            continue

        ids = _read_property_ids(data, arr_off, cprops)
        if ids is None:
            continue

        target_idx = (pid_offset - arr_off) // 4
        if (ids[target_idx] >> 6) != (_PROPERTY_ID_RICH_EDIT_TEXT_UNICODE >> 6):
            continue

        return start, ids, arr_end
    return None


def _decode_richtext_after(data: bytes, pid_offset: int) -> str | None:
    found = _find_propertyset_start(data, pid_offset)
    if found is None:
        return None
    start, ids, pos = found
    target_idx = (pid_offset - (start + 2)) // 4

    for i in range(target_idx):
        width = _property_value_width(data, pos, ids[i] >> 26)
        if width is None:
            return None
        pos += width

    if pos + 4 > len(data):
        return None
    length = struct.unpack_from("<I", data, pos)[0]
    if length <= 0 or length > 8000 or length % 2 != 0:
        return None
    text_off = pos + 4
    if text_off + length > len(data):
        return None

    try:
        text = data[text_off:text_off + length].decode("utf-16-le")
    except UnicodeDecodeError:
        return None
    return text.rstrip("\x00")


def extract_paragraph_texts(data: bytes) -> list[tuple[int, str]]:
    """Find every RichEditTextUnicode property and decode its real text.

    Returns (offset, text) pairs in file order, unfiltered and
    undeduplicated (same contract as extract_runs) -- callers run this
    through the same filter_noise/dedupe_revisions/guess_pages pipeline.
    """
    pid_bytes = struct.pack("<I", _PROPERTY_ID_RICH_EDIT_TEXT_UNICODE)
    results: list[tuple[int, str]] = []
    idx = 0
    while True:
        idx = data.find(pid_bytes, idx)
        if idx == -1:
            break
        text = _decode_richtext_after(data, idx)
        if text:
            results.append((idx, text))
        idx += 1
    return results


# --- noise filtering (heuristic fallback) -----------------------------------

_KNOWN_NOISE_TOKENS = {
    "Calibri", "Calibri Light", "Consolas", "Segoe UI", "Segoe UI Semibold",
    "Arial", "cite", "code", "citation", "Normal", "footer", "header",
    "PageTitle", "PageDateTime", "PageTitleDate", "PageTitleTime",
    "blockquote", "quote", "strong", "em",
}

_GUID_RE = re.compile(r"^\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                       r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}$")
_ALLDIGITS_RE = re.compile(r"^\d{1,12}$")
_CAMELCASE_RE = re.compile(r"^[A-Z][a-z]+(?:[A-Z][a-z]+)+$")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
_DATE_RE = re.compile(
    r"^\w+\s+\d{1,2}\.\s*\w+\s+\d{4}$|^\d{1,2}\.\s*\w+\s+\d{4}$"
)

# Frequency-based catch-all: tokens that are short and recur often across a
# wide span of the file are probably style/metadata (a font name, an
# author name) rather than content that happens to repeat. Frequency/span
# alone can't reliably separate genuine repeated content from noise across
# all file sizes -- a real fix would need the actual Author PropertyID,
# the same kind of property-based approach used for CachedTitleString.
_NOISE_MAX_LEN = 30
_NOISE_MIN_FREQ = 4
_NOISE_MIN_SPAN = 20000


def _looks_like_noise_token(text: str) -> bool:
    if text in _KNOWN_NOISE_TOKENS:
        return True
    if _GUID_RE.match(text):
        return True
    if _ALLDIGITS_RE.match(text):
        return True
    if _CAMELCASE_RE.match(text.strip()):
        return True
    if _TIME_RE.match(text.strip()):
        return True
    if _DATE_RE.match(text.strip()):
        return True
    return False


def filter_noise(runs: list[tuple[int, str]],
                  keep: frozenset[str] = frozenset(),
                  wide_spread_check: bool = True) -> list[tuple[int, str]]:
    """Drop style/metadata noise runs.

    `keep` exempts specific exact strings from the wide-spread-repetition
    check below -- namely the section's own filename stem, which is short
    and (deliberately) repeats across a wide span of the file as a page
    manifest marker (see find_manifest_titles).

    `wide_spread_check=False` disables that repetition check entirely. Used
    for the pass that feeds find_manifest_titles: a real page title can
    *also* legitimately appear as ordinary vocabulary elsewhere in the file,
    which triggers the same "short + repeats across a wide span" pattern as
    noise -- pre-filtering those out would hide genuine titles from the
    manifest scan before `keep` even gets a chance to protect them.
    """
    from collections import defaultdict

    occurrences: dict[str, list[int]] = defaultdict(list)
    for off, text in runs:
        occurrences[text].append(off)

    wide_spread_noise: set[str] = set()
    if wide_spread_check:
        for text, offsets in occurrences.items():
            if text in keep:
                continue
            if len(text) <= _NOISE_MAX_LEN and len(offsets) >= _NOISE_MIN_FREQ:
                span = max(offsets) - min(offsets)
                if span >= _NOISE_MIN_SPAN:
                    wide_spread_noise.add(text)

    out = []
    for off, text in runs:
        stripped = text.strip()
        if not stripped:
            continue
        if _looks_like_noise_token(stripped):
            continue
        if text in wide_spread_noise:
            continue
        out.append((off, text))
    return out


# --- OneNote edit-history de-duplication (heuristic fallback) --------------

def dedupe_revisions(runs: list[tuple[int, str]]) -> list[tuple[int, int, str]]:
    """Collapse OneNote's incremental edit-history duplicates.

    OneNote keeps every intermediate revision of a paragraph as it's typed
    or edited, so the same text tends to reappear multiple times, each copy
    a prefix (or near-prefix) of the next, growing towards the final text.
    We keep only the longest variant in each such family, at the position
    of its first occurrence -- but remember how many raw occurrences fed
    into it, since that count is later used as a page-title signal (see
    guess_pages): a real page title tends to be re-stored a few times in a
    way an ordinary body paragraph typically isn't.

    Returns (offset, occurrence_count, text) tuples, sorted by offset.
    """
    by_len = sorted(runs, key=lambda r: len(r[1]), reverse=True)
    kept: list[list] = []  # [offset, count, text]

    def find_family(text: str):
        for entry in kept:
            kt = entry[2]
            if text == kt:
                return entry
            # Prefix/substring matching is only safe for longer strings: a
            # short bullet is often coincidentally a prefix of an unrelated,
            # separate short bullet rather than an earlier edit of the same
            # paragraph.
            if len(text) >= 20 and (kt.startswith(text) or kt.endswith(text)):
                return entry
            probe = text[:30]
            if len(probe) >= 20 and probe in kt:
                return entry
        return None

    for off, text in by_len:
        family = find_family(text)
        if family is not None:
            family[1] += 1
        else:
            kept.append([off, 1, text])

    kept.sort(key=lambda r: r[0])
    return [(off, count, text) for off, count, text in kept]


# --- hyperlink reconstruction ----------------------------------------------

_HYPERLINK_RE = re.compile(r'^HYPERLINK\s+"([^"]+)"(.*)$', re.DOTALL)


def render_run(text: str) -> str:
    m = _HYPERLINK_RE.match(text)
    if m:
        url, label = m.group(1), m.group(2).strip()
        if not label or label == url:
            return url
        return f"[{label}]({url})"
    return text.strip()


# --- page-boundary heuristics (fallback path only) --------------------------

_TITLE_MAX_LEN = 100


@dataclass
class Page:
    title: str
    title_offset: int
    # (indent_depth, text) pairs. The heuristic fallback path has no real
    # nesting information and always uses depth 0; the real
    # onestore_page.py path supplies genuine depths from the object tree.
    body: list[tuple[int, str]]


def _looks_titleish(text: str) -> bool:
    return (
        len(text) <= _TITLE_MAX_LEN
        and "\n" not in text
        and not text.endswith(('.', ',', ':', ';'))
    )


_MANIFEST_WINDOW = 800
_SECTION_IDENTITY_WINDOW = 150


def find_section_identity_candidates(raw_runs: list[tuple[int, str]]
                                      ) -> list[tuple[int, str]]:
    """Find "this section used to be called X" history entries.

    Whenever a section gets renamed (or was briefly saved under a
    provisional name before settling), the old name and its would-be
    filename (`X` followed shortly by `X.one`) linger in the file as a
    distinct kind of breadcrumb -- structurally identical to the page-title
    markers find_manifest_titles looks for, but for the *section itself*,
    not a page.

    Returns (offset, name) pairs in file order.
    """
    sorted_runs = sorted(raw_runs, key=lambda r: r[0])
    candidates: list[tuple[int, str]] = []
    for off, text in sorted_runs:
        name = text.strip()
        if not name:
            continue
        expected = (name + ".one").lower()
        for off2, text2 in sorted_runs:
            if off2 <= off or off2 - off > _SECTION_IDENTITY_WINDOW:
                continue
            if text2.strip().lower() == expected:
                candidates.append((off, name))
                break
    return candidates


def find_manifest_titles(raw_runs: list[tuple[int, str]],
                          section_stem: str) -> set[str]:
    """Find real page titles via OneNote's in-file page manifest.

    Every occurrence of the section's own filename stem as a short
    standalone run sits immediately next to that page's actual title.
    """
    stem_lower = section_stem.strip().lower()
    if not stem_lower:
        return set()

    section_identity_names: set[str] = set()
    for _off, name in find_section_identity_candidates(raw_runs):
        section_identity_names.add(name.lower())
        section_identity_names.add((name + ".one").lower())

    stem_offsets = [off for off, text in raw_runs if text.strip().lower() == stem_lower]
    titles: set[str] = set()
    for stem_off in stem_offsets:
        for off, text in raw_runs:
            if abs(off - stem_off) > _MANIFEST_WINDOW:
                continue
            rendered = render_run(text)
            if rendered.lower() == stem_lower or not _looks_titleish(rendered):
                continue
            if rendered.lower() in section_identity_names:
                continue
            titles.add(rendered)

    return _drop_bleedthrough_variants(titles)


def _drop_bleedthrough_variants(titles: set[str]) -> set[str]:
    """Drop titles that are probably scan/decode artifacts of another,
    shorter title rather than a real distinct one.

    Guarded by a word-boundary check: e.g. "Page10" legitimately starts
    with "Page1" (a *different* real title), so only treat this as
    bleed-through when the longer text's next character isn't
    alphanumeric.
    """
    titles = set(titles)
    for longer in list(titles):
        for shorter in titles:
            if shorter == longer or not longer.startswith(shorter):
                continue
            boundary = longer[len(shorter):len(shorter) + 1]
            if boundary and boundary.isalnum():
                continue
            titles.discard(longer)
            break
    return titles


def find_page_titles(data: bytes, raw_runs: list[tuple[int, str]],
                      section_stem: str) -> set[str]:
    """Real page titles for this section, preferring the actual
    CachedTitleString property over the text-adjacency heuristic."""
    titles = {
        value for _off, value in
        extract_property_strings(data, _PROPERTY_ID_CACHED_TITLE_STRING)
    }
    if titles:
        return _drop_bleedthrough_variants(titles)
    return find_manifest_titles(raw_runs, section_stem)


def guess_pages(raw_runs: list[tuple[int, str]],
                 manifest_titles: set[str],
                 section_stem: str = "") -> list[Page]:
    """Group a section's raw (pre-dedup) text runs into inferred pages
    (heuristic fallback path -- see extract_section)."""
    stem = section_stem.strip() or "page"
    stem_lower = stem.lower()

    raw_runs = sorted(raw_runs, key=lambda r: r[0])

    if manifest_titles:
        title_positions = [
            (off, render_run(text)) for off, text in raw_runs
            if render_run(text) in manifest_titles
        ]
    else:
        globally_deduped = dedupe_revisions(raw_runs)
        title_positions = [
            (off, render_run(text)) for off, count, text in globally_deduped
            if count >= 2 and _looks_titleish(render_run(text))
            and render_run(text).lower() != stem_lower
        ]

    if not title_positions:
        deduped = dedupe_revisions(raw_runs)
        body = [render_run(text) for _off, _count, text in deduped]
        return [Page(title=stem, title_offset=raw_runs[0][0] if raw_runs else 0,
                     body=[(0, b) for b in body if b])]

    title_offsets_sorted = [off for off, _title in title_positions]
    title_offsets_set = set(title_offsets_sorted)

    def nearest_title(off: int) -> str:
        idx = bisect.bisect_left(title_offsets_sorted, off)
        best_idx = idx
        if idx == len(title_offsets_sorted):
            best_idx = idx - 1
        elif idx > 0:
            before_off = title_offsets_sorted[idx - 1]
            after_off = title_offsets_sorted[idx]
            best_idx = idx - 1 if (off - before_off) <= (after_off - off) else idx
        return title_positions[best_idx][1]

    order: list[str] = []
    first_offset: dict[str, int] = {}
    for off, title in title_positions:
        if title not in first_offset:
            first_offset[title] = off
            order.append(title)

    body_runs_by_title: dict[str, list[tuple[int, str]]] = {t: [] for t in order}
    for off, text in raw_runs:
        if off in title_offsets_set:
            continue
        rendered = render_run(text)
        if not rendered or rendered.lower() == stem_lower:
            continue
        body_runs_by_title[nearest_title(off)].append((off, text))

    pages: list[Page] = []
    for title in order:
        deduped = dedupe_revisions(body_runs_by_title[title])
        body = [render_run(text) for _off, _count, text in deduped]
        pages.append(Page(title=title, title_offset=first_offset[title],
                           body=[(0, b) for b in body if b]))

    return pages


# --- section/notebook discovery ---------------------------------------------

def find_section_files(notebook_dir: Path) -> list[Path]:
    return sorted(
        p for p in notebook_dir.glob("*.one")
        if p.is_file()
    )


def find_toc_file(notebook_dir: Path) -> Path | None:
    candidates = list(notebook_dir.glob("*.onetoc2"))
    return candidates[0] if candidates else None


def toc_section_order(toc_path: Path) -> list[str]:
    """Best-effort ordered list of '*.one' filenames referenced by the TOC."""
    data = toc_path.read_bytes()
    runs = extract_runs(data, min_chars=3)
    seen: list[str] = []
    for _, text in runs:
        if text.lower().endswith(".one") and text not in seen:
            seen.append(text)
    return seen


# --- top-level extraction ---------------------------------------------------

def _extract_section_real(path: Path) -> list[Page] | None:
    """Real page order/content via genuine [MS-ONESTORE]/[MS-FSSHTTPB]
    object-tree resolution (see onestore_page.py). Returns None if this
    file doesn't decode that way, or the walk hits a structure not yet
    handled -- callers fall back to the heuristic extractor in that case."""
    try:
        real_pages = onestore_page.extract_pages(path)
    except Exception as exc:
        print(f"warning: real object-tree extraction failed for {path.name}"
              f" ({exc.__class__.__name__}: {exc}), falling back to heuristic",
              file=sys.stderr)
        return None
    if real_pages is None:
        return None
    return [
        Page(title=p.title, title_offset=0, body=list(p.lines))
        for p in real_pages
    ]


def extract_section(path: Path) -> list[Page]:
    real = _extract_section_real(path)
    if real is not None:
        return real
    return _extract_section_heuristic(path)


def _extract_section_heuristic(path: Path) -> list[Page]:
    data = path.read_bytes()
    all_runs = extract_runs(data, min_chars=3)

    unfiltered = filter_noise(all_runs, wide_spread_check=False)
    manifest_titles = find_page_titles(data, unfiltered, path.stem)

    paragraph_runs = extract_paragraph_texts(data)
    title_runs = extract_property_strings(data, _PROPERTY_ID_CACHED_TITLE_STRING)
    return guess_pages(paragraph_runs + title_runs, manifest_titles,
                        section_stem=path.stem)


def section_display_name(path: Path) -> str:
    """The section's real display name, combining two independent
    sources and trusting whichever saw the most recent rename:

    - The real SectionDisplayName property.
    - The name-immediately-followed-by-name.one rename-history heuristic,
      which empirically catches later renames the property scan misses.

    Whichever source's last (highest-offset) occurrence is furthest into
    the file wins, on the theory that later file position corresponds to
    a later point in the section's edit history.
    """
    data = path.read_bytes()
    candidates: list[tuple[int, str]] = list(
        extract_property_strings(data, _PROPERTY_ID_SECTION_DISPLAY_NAME)
    )

    unfiltered = filter_noise(extract_runs(data, min_chars=3), wide_spread_check=False)
    candidates.extend(find_section_identity_candidates(unfiltered))

    if candidates:
        return max(candidates, key=lambda c: c[0])[1]
    return path.stem


def _render_body(body: list[tuple[int, str]]) -> list[str]:
    """Render a page's paragraphs as a Markdown bullet list, indented by
    real depth where available -- falls back to depth 0 (flat) for pages
    that went through the heuristic extractor instead."""
    return [f"{'    ' * depth}- {para}" for depth, para in body]


def render_section(path: Path) -> str:
    """Render a single .one file on its own (no sibling TOC needed)."""
    lines: list[str] = [f"# {path.name}\n"]
    pages = [p for p in extract_section(path) if p.body]
    for page in pages:
        lines.append(f"## {page.title}\n")
        lines.extend(_render_body(page.body))
        lines.append("")
    return "\n".join(lines)


def render_notebook(notebook_dir: Path) -> str:
    toc_path = find_toc_file(notebook_dir)
    section_files = find_section_files(notebook_dir)
    on_disk = {p.name: p for p in section_files}

    ordered_names: list[str]
    if toc_path is not None:
        ordered_names = [n for n in toc_section_order(toc_path) if n in on_disk]
        for n in on_disk:
            if n not in ordered_names:
                ordered_names.append(n)
    else:
        ordered_names = list(on_disk.keys())

    sections: list[tuple[str, Path, list[Page]]] = []
    for name in ordered_names:
        path = on_disk[name]
        sections.append((section_display_name(path), path, extract_section(path)))

    lines: list[str] = [f"# Notebook: {notebook_dir.name}\n"]

    for section_title, path, pages in sections:
        non_empty_pages = [p for p in pages if p.body]
        if not non_empty_pages:
            continue

        lines.append(f"## Section: {section_title}  ({path.name})\n")

        for page in non_empty_pages:
            lines.append(f"### {page.title}\n")
            lines.extend(_render_body(page.body))
            lines.append("")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", type=Path,
                     help="A OneNote notebook folder (containing .one / "
                          ".onetoc2 files), or a single .one/.onetoc2 file")
    ap.add_argument("-o", "--output", type=Path, default=None,
                     help="Write Markdown output here instead of stdout")
    args = ap.parse_args()

    path = args.path
    if path.is_dir():
        output = render_notebook(path)
    elif path.is_file() and path.suffix.lower() == ".one":
        output = render_section(path)
    elif path.is_file() and path.suffix.lower() == ".onetoc2":
        output = render_notebook(path.parent)
    else:
        print(f"error: {path} is not a notebook folder or a .one/.onetoc2 "
              f"file", file=sys.stderr)
        return 1

    if args.output:
        args.output.write_text(output, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(output)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
