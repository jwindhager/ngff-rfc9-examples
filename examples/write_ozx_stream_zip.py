#!/usr/bin/env python3
"""Pack an OME-Zarr directory into an OME-Zarr zip file (.ozx) with the third-party
`stream-zip` package (pip install stream-zip; tested with 0.0.84; not based on zipfile).

    python3 write_ozx_stream_zip.py path/to/image.ome.zarr out.ozx

Compared to `write_ozx.py` (standard library `zipfile`):
  * ZIP64 end of central directory records AND ZIP64 extra fields on every entry are
    written when the member method is a *_64 method (RFC-9 ZIP64 recommendations 1 and 2).
  * STORE: NO_COMPRESSION_64.
  * `extended_timestamps=False` avoids the 0x5455 extra field (which RFC-9 permits).
  * Members are written, and listed in the central directory, in the order given, so
    zarr.json files have to be yielded first.
  * The package has no option for the ZIP archive comment, so `set_zip_comment` below
    appends it by patching the last two bytes of the end record. The same function works for
    any zip file without an archive comment, e.g. one written by the JDK zip file system.
"""
import datetime
import json
import os
import struct
import sys

from stream_zip import NO_COMPRESSION_64, stream_zip


def set_zip_comment(path, comment: bytes):
    """Set the archive comment of a ZIP file that has none (comment length 0)."""
    if len(comment) > 0xFFFF:
        raise ValueError("ZIP comments are limited to 65535 bytes")
    with open(path, "r+b") as f:
        f.seek(-22, os.SEEK_END)
        end = f.read(22)
        if end[:4] != b"PK\x05\x06" or struct.unpack("<H", end[20:22])[0] != 0:
            raise ValueError("expected an end of central directory record without a comment at the end of the file")
        f.seek(-2, os.SEEK_END)
        f.write(struct.pack("<H", len(comment)) + comment)


def members(root):
    files = []
    for r, _, names in os.walk(root):
        for n in names:
            files.append((os.path.relpath(os.path.join(r, n), root).replace(os.sep, "/"), os.path.join(r, n)))
    # zarr.json records first (root first, then breadth-first), the rest after
    files.sort(key=lambda t: (not t[0].endswith("zarr.json"), t[0].count("/"), t[0]))
    for arcname, full in files:
        def chunks(full=full):
            with open(full, "rb") as f:
                while True:
                    b = f.read(1 << 20)
                    if not b:
                        break
                    yield b
        modified = datetime.datetime.fromtimestamp(os.path.getmtime(full))
        yield arcname, modified, 0o644, NO_COMPRESSION_64, chunks()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    with open(sys.argv[2], "wb") as out:
        for chunk in stream_zip(members(sys.argv[1]), extended_timestamps=False):
            out.write(chunk)
    set_zip_comment(sys.argv[2], json.dumps(
        {"ome": {"version": "0.5", "zipFile": {"centralDirectory": {"jsonFirst": True}}}},
        separators=(",", ":")).encode("utf-8"))
