#!/usr/bin/env python3
"""Pack an OME-Zarr directory into an OME-Zarr zip file (.ozx), RFC-9 style.

Standard library only (tested with Python 3.9). Usage:

    python3 write_ozx.py path/to/image.ome.zarr out.ozx [--version 0.5]

What it does, with the RFC-9 requirement it addresses:
  * every entry is STORED (no ZIP-level compression)           MUST
  * no extra fields except ZIP64 (ZipInfo has none by default)  MUST
  * nothing before the first local file header                  MUST
  * ZIP64 is requested for every entry (force_zip64)            SHOULD
  * zarr.json records are sorted to the front of the central
    directory (root first, then breadth-first), the entries
    themselves are written in any order                         SHOULD
  * archive comment holds {"ome": {"version", "zipFile": ...}}  SHOULD

Notes / limitations of the standard library (see the RFC guidance text):
  * zipfile writes the central directory in the order of ZipFile.filelist.
    Sorting that list before close() is the only way to control the order;
    it is a documented attribute only through infolist(), which returns the
    same list object in CPython. Treat it as an implementation detail.
  * `zf.comment` must be bytes and at most 65535 bytes.
"""
import json
import os
import shutil
import sys
import zipfile


def zarr_json_key(name):
    """Sort key: root zarr.json first, then other zarr.json by depth (breadth-first)."""
    depth = name.count("/")
    return (0, depth, name)


def pack(src_dir, out_path, ome_version="0.5"):
    # collect files: arcnames use "/" and are relative to the Zarr root
    files = []
    for root, _, names in os.walk(src_dir):
        for n in names:
            full = os.path.join(root, n)
            files.append((os.path.relpath(full, src_dir).replace(os.sep, "/"), full))
    if not any(a == "zarr.json" for a, _ in files):
        raise SystemExit("zarr.json not found at the root of the Zarr hierarchy")

    # data first, metadata last is fine: the central directory is sorted below
    files.sort(key=lambda t: (t[0].endswith("zarr.json"), t[0]))

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for arcname, full in files:
            zi = zipfile.ZipInfo.from_file(full, arcname)
            zi.compress_type = zipfile.ZIP_STORED          # MUST: no ZIP-level compression
            with open(full, "rb") as src, zf.open(zi, "w", force_zip64=True) as dst:  # SHOULD: ZIP64
                shutil.copyfileobj(src, dst, 1024 * 1024)

        # SHOULD: all zarr.json records precede all other records in the central directory
        jsons = sorted((i for i in zf.filelist if i.filename.endswith("zarr.json")),
                       key=lambda i: zarr_json_key(i.filename))
        others = [i for i in zf.filelist if not i.filename.endswith("zarr.json")]
        zf.filelist[:] = jsons + others

        zf.comment = json.dumps(
            {"ome": {"version": ome_version,
                     "zipFile": {"centralDirectory": {"jsonFirst": True}}}},
            separators=(",", ":")).encode("utf-8")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    version = sys.argv[sys.argv.index("--version") + 1] if "--version" in sys.argv else "0.5"
    if "--version" in sys.argv:
        args.remove(version)
    if len(args) != 2:
        raise SystemExit(__doc__)
    pack(args[0], args[1], version)
