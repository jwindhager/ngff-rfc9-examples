#!/usr/bin/env python3
"""Check an OME-Zarr zip file (.ozx) against the MUST / SHOULD rules of RFC-9.

Works on a local path or an http(s) URL (only range requests are used: the
end of the file and the central directory are read, never the entry data).

Checks of the central directory (MUST unless noted):
  - root `zarr.json` present
  - every entry uses STORE (method 0)
  - no encryption flag
  - no extra fields other than 0x0001 (ZIP64), 0x5455 (extended timestamp),
    0x7875 (Info-ZIP New Unix)
  - single volume (disk numbers 0)
  - no data before the first local file header (lowest local header offset is 0)
SHOULD / informational:
  - ZIP64 records in use
  - all zarr.json records precede all other records, ignoring directory entries
    (names ending in '/') (jsonFirst order), and
    whether the comment says so (a `jsonFirst: true` comment on an unordered
    archive is an inconsistency)
  - duplicate names in the central directory
  - archive comment is JSON with an `ome` object holding a string `version`
  - extension is `.ozx`
Extra fields in the local file headers are not checked (that would need one
request per entry); only the central directory records are checked.

Usage: python3 check_ozx.py PATH_OR_URL
Exit status 1 if a MUST is violated. Standard library only.
"""
import json
import struct
import sys
import urllib.request

ALLOWED_EXTRA = {0x0001: "ZIP64", 0x5455: "extended timestamp", 0x7875: "Info-ZIP New Unix"}
CD_STRUCT = struct.Struct("<4s6H3L5H2L")


class Source:
    def __init__(self, loc):
        self.loc = loc
        self.http = loc.startswith(("http://", "https://"))
        self.requests = 0
        if self.http:
            req = urllib.request.Request(loc, headers={"Range": "bytes=0-0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                cr = r.headers.get("Content-Range")
                self.size = int(cr.split("/")[1]) if cr else int(r.headers["Content-Length"])
        else:
            import os
            self.size = os.path.getsize(loc)

    def read(self, off, n):
        self.requests += 1
        n = min(n, self.size - off)
        if self.http:
            req = urllib.request.Request(self.loc, headers={"Range": f"bytes={off}-{off + n - 1}"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        with open(self.loc, "rb") as f:
            f.seek(off)
            return f.read(n)


def read_directory(src):
    tail_len = min(src.size, 65536 + 22 + 20 + 56)
    tail = src.read(src.size - tail_len, tail_len)
    i = tail.rfind(b"PK\x05\x06")
    if i < 0:
        raise SystemExit("no end of central directory record")
    (_, disk, cd_disk, n_disk, n_total, cd_size, cd_off, clen) = struct.unpack("<4s4H2LH", tail[i:i + 22])
    comment = tail[i + 22:i + 22 + clen]
    zip64 = False
    if n_total == 0xFFFF or cd_size == 0xFFFFFFFF or cd_off == 0xFFFFFFFF:
        j = tail.rfind(b"PK\x06\x07", 0, i)
        _, _, rec_off, _ = struct.unpack("<4sLQL", tail[j:j + 20])
        rec = src.read(rec_off, 56)
        (_, _, _, _, _, _, n_total, _, cd_size, cd_off) = struct.unpack("<4sQ2H2L4Q", rec)
        zip64 = True
    elif tail.rfind(b"PK\x06\x07", 0, i) >= 0:
        zip64 = True
    cd = src.read(cd_off, cd_size)
    entries, pos = [], 0
    while pos + 46 <= len(cd):
        (sig, _, _, flags, method, _, _, _, csize, usize, nlen, elen, clen2,
         dstart, _, _, off) = CD_STRUCT.unpack_from(cd, pos)
        assert sig == b"PK\x01\x02", "bad central directory signature"
        name = cd[pos + 46:pos + 46 + nlen].decode("utf-8")
        ex = cd[pos + 46 + nlen:pos + 46 + nlen + elen]
        ids, k = [], 0
        while k + 4 <= len(ex):
            hid, hsz = struct.unpack_from("<HH", ex, k)
            ids.append(hid)
            if hid == 0x0001 and off == 0xFFFFFFFF:
                p = k + 4 + (8 if usize == 0xFFFFFFFF else 0) + (8 if csize == 0xFFFFFFFF else 0)
                off = struct.unpack_from("<Q", ex, p)[0]
            k += 4 + hsz
        entries.append(dict(name=name, flags=flags, method=method, extra=ids, disk=dstart, off=off))
        pos += 46 + nlen + elen + clen2
    return dict(disk=disk, cd_disk=cd_disk, n_total=n_total, zip64=zip64,
                comment=comment, entries=entries, cd_size=cd_size)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit(__doc__)
    loc = args[0]
    src = Source(loc)
    d = read_directory(src)
    E = d["entries"]
    must, should, info = [], [], []

    names = [e["name"] for e in E]
    if "zarr.json" not in names:
        must.append("root-level zarr.json is missing")
    bad = [e["name"] for e in E if e["method"] != 0]
    if bad:
        must.append(f"{len(bad)} entries are not STORE, e.g. {bad[0]} (method {[e['method'] for e in E if e['method'] != 0][0]})")
    if any(e["flags"] & 1 for e in E):
        must.append("encrypted entries")
    extra = {}
    for e in E:
        for h in e["extra"]:
            if h not in ALLOWED_EXTRA:
                extra.setdefault(h, e["name"])
    for h, n in extra.items():
        must.append(f"disallowed extra field 0x{h:04x} (first seen on {n})")
    if d["disk"] or d["cd_disk"] or any(e["disk"] for e in E):
        must.append("multi-volume archive")
    first = min((e["off"] for e in E), default=0)
    if first != 0:
        must.append(f"data before the first local file header (lowest offset {first})")
    else:
        sig = src.read(0, 4)
        if sig != b"PK\x03\x04":
            must.append("no local file header at offset 0")

    if not d["zip64"]:
        should.append("ZIP64 end of central directory records are not present")
    n_zip64_cd = sum(1 for e in E if 1 in e["extra"])
    info.append(f"ZIP64 end records: {d['zip64']}; ZIP64 extra field in {n_zip64_cd}/{len(E)} "
                "central directory records (local header extra fields are not checked)")
    seen, dups = set(), set()
    for n in names:
        (dups if n in seen else seen).add(n)
    if dups:
        should.append(f"{len(dups)} duplicate names in the central directory, e.g. {sorted(dups)[0]}")
    is_json = [n.endswith("zarr.json") for n in names if not n.endswith("/")]
    ordered = all(is_json[i] >= is_json[i + 1] for i in range(len(is_json) - 1))

    comment_ok, jf = False, None
    if d["comment"]:
        try:
            c = json.loads(d["comment"].decode("utf-8"))
            ome = c.get("ome")
            comment_ok = isinstance(ome, dict) and isinstance(ome.get("version"), str)
            jf = bool(((ome or {}).get("zipFile") or {}).get("centralDirectory", {}).get("jsonFirst"))
        except Exception:
            pass
    if not comment_ok:
        should.append("archive comment is missing or is not JSON with ome.version")
    if jf and not ordered:
        should.append("comment says jsonFirst: true but zarr.json records do not all precede other records")
    if not loc.split("?")[0].endswith(".ozx"):
        should.append("file name does not end with .ozx")
    info.append(f"{len(E)} entries, central directory {d['cd_size']} B, "
                f"{src.requests} read requests, zarr.json records first in order: {ordered}, "
                f"comment jsonFirst: {jf}, extra field ids used: "
                f"{sorted({h for e in E for h in e['extra']}) or 'none'}")
    print(f"== {loc}")
    for m in must:
        print(f"  MUST violated : {m}")
    for s in should:
        print(f"  SHOULD/notice : {s}")
    for i in info:
        print(f"  info          : {i}")
    print("  RESULT        :", "NOT CONFORMING" if must else "no MUST violations found (central directory only)")
    return 1 if must else 0


if __name__ == "__main__":
    sys.exit(main())
