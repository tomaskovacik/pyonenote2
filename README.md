# pyonenote2

A from-spec Python reader for OneNote `.one` / `.onetoc2` files.

Modern `.one` section files synced through OneDrive/SharePoint don't use
the classic flat MS-ONESTORE `FileNodeList` container the public spec
describes for standalone files — they embed a real
[\[MS-FSSHTTPB\]](https://learn.microsoft.com/en-us/openspecs/sharepoint_protocols/ms-fsshttpb/)
(File Synchronization via SOAP over HTTP, Binary) Data Element Package
instead. This library implements enough of MS-FSSHTTPB and
[\[MS-ONESTORE\]](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-onestore/)
to walk that real object graph: real page order and real per-page content
(with correct outline nesting), recovered by actually resolving object
references — not by guessing page boundaries from text-run proximity.

## Status

Alpha. Developed and verified against a real-world multi-section notebook
synced via OneDrive/SharePoint. The core path (page discovery, real
content-tree walk, hyperlink text) is solid; known gaps:

- `Data Element Hash` and `Object Metadata Declaration` (both optional
  MS-FSSHTTPB structures) aren't implemented yet — if a file uses them,
  extraction for that file raises and the library falls back to a
  heuristic UTF-16LE text-run scanner instead (page boundaries become a
  guess, and outline nesting depth is lost).
- `.onetoc2` (the notebook table of contents) is read separately and more
  simply: in the files this was developed against it still uses the
  classic flat FileNodeList container, so section discovery/ordering is
  currently a lightweight text-run scan for `*.one` filenames rather than
  a full FileNodeList walk.
- Tables, images, ink, and math are not extracted — text content only.

## Install

```bash
pip install -e .
```

## Usage

```bash
# A single section file
pyonenote2 "My Notebook/Section 1.one"

# A whole notebook folder (or point at its .onetoc2 directly)
pyonenote2 "My Notebook/" -o notebook.md
```

As a library:

```python
from pathlib import Path
from pyonenote2 import extract_section, render_notebook

pages = extract_section(Path("Section 1.one"))
for page in pages:
    print(page.title)
    for depth, text in page.body:
        print("  " * depth + "- " + text)

# Or render a whole notebook folder to Markdown directly:
markdown = render_notebook(Path("My Notebook/"))
```

## How it works

1. **`fsshttpb.py`** — low-level MS-FSSHTTPB primitives: Compact Unsigned
   64-bit Integer, Extended GUID (all size variants), Serial Number, Cell
   ID, Stream Object Headers, and the Data Element Package itself
   (Storage Index, Storage Manifest, Cell Manifest, Revision Manifest,
   Object Group, Object Data BLOB).
2. **`onestore_objects.py`** — MS-ONESTORE object/cell/revision
   resolution on top of that: follows the spec's own reading algorithm
   (Storage Manifest → Data root → Storage Index cell/revision mapping →
   Cell Manifest → Revision Manifest delta chain) to get the current,
   fully-merged set of objects for any cell, and resolves `CompactID`
   references via the section 2.7.8 Mapping Table.

   The MS-ONESTORE spec text describes *how to build* the Mapping Table
   but not precisely how it's used to resolve a reference — that part was
   cross-checked against the
   [msiemens/onenote.rs](https://github.com/msiemens/onenote.rs) Rust
   implementation (`onestore/fsshttpb/object.rs`,
   `onestore/fsshttpb/mapping_table.rs`) and confirmed: OID-stream
   `CompactID`s are matched *positionally* against the Object Data's own
   Extended GUID Array, and OSID/ContextID-stream `CompactID`s are
   matched positionally against its Cell ID Array, split by whether each
   entry's object space matches the *current* object space.
3. **`onestore_page.py`** — walks from a section's content root through
   `ElementChildNodes` (real page list, in real order) and, per page,
   through its own separate cell's content tree
   (`ContentChildNodes`/`ElementChildNodes`) to recover real paragraph
   text with real outline depth.
4. **`extract.py`** — CLI + Markdown rendering, with a heuristic
   text-run-scanning fallback for files/structures the real path doesn't
   (yet) handle.

## Acknowledgments

The MS-FSSHTTPB Mapping Table resolution algorithm was cross-checked
against [msiemens/onenote.rs](https://github.com/msiemens/onenote.rs)
(MIT licensed) — thank you to its authors and contributors for publishing
a working reference implementation.

## License

MIT
