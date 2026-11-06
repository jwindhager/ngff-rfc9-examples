#!/usr/bin/env python3
"""Check an OME-Zarr zip file (.ozx) against the MUST / SHOULD rules of RFC-9.

Works on a local path or an http(s) URL (only range requests are used: the
end of the file, the central directory and the first local file header are
read, never the entry data).

Checks of the central directory (MUST unless noted):
  - root `zarr.json` present
  - every entry uses STORE (method 0)
  - no encryption of entries: general purpose bit 0 (encrypted) and bit 6
    (strong encryption) are clear
  - no encryption of the central directory, detected by any of
      * the central directory does not start with a plain central directory
        record (an encrypted central directory starts with an archive
        decryption header),
      * the ZIP64 end of central directory record needs version 6.2
        ("central directory encryption"),
      * general purpose bit 13 (masked local header values) is set in a
        central directory record or in the first local file header
    These are indicators: the checker cannot decrypt anything.
  - no extra field that is known to be non-compliant (see NONCOMPLIANT_EXTRA):
    extra fields must not change how the bytes of an entry are interpreted
  - single volume (disk numbers 0)
  - no data before the first local file header (lowest local header offset is 0)
SHOULD / informational:
  - extra fields that are on neither the compliant list (COMPLIANT_EXTRA) nor
    the non-compliant list cannot be verified and are reported
  - an Info-ZIP Unicode Path extra field (0x7075) that differs from the file name
  - ZIP64 records in use
  - all zarr.json records precede all other records, ignoring directory entries
    (names ending in '/') (jsonFirst order), and
    whether the comment says so (a `jsonFirst: true` comment on an unordered
    archive is an inconsistency)
  - duplicate names in the central directory
  - archive comment is JSON with an `ome` object holding a string `version`
  - extension is `.ozx`
Extra fields in the local file headers are not checked (that would need one
request per entry); only the central directory records are checked. The flags
of the first local file header are checked as well.

The header IDs are from the PKWARE APPNOTE 6.3.10 (sections 4.5 and 4.6).

Usage: python3 check_ozx.py PATH_OR_URL
Exit status 1 if a MUST is violated. Standard library only.
"""
import json
import struct
import sys
import urllib.request
import zlib

# Extra fields known to be compliant: metadata, alignment or padding that does not
# change how the bytes of an entry are interpreted (and, for 0x7075 and 0x6375, the
# UTF-8 form of the name and comment stored in the header).
COMPLIANT_EXTRA = {
    0x0001: "ZIP64 extended information",
    0x000A: "NTFS timestamps",
    0x5455: "extended timestamp",
    0x5855: "Info-ZIP UNIX (original)",
    0x7855: "Info-ZIP UNIX",
    0x7875: "Info-ZIP New Unix",
    0xA11E: "data stream alignment (Apache Commons Compress)",
    0xA220: "Microsoft Open Packaging growth hint",
    0xCAFE: "Java JAR header",
    0xD935: "Android ZIP alignment",
    0x7075: "Info-ZIP Unicode Path",
    0x6375: "Info-ZIP Unicode Comment",
}
# Extra fields known to be non-compliant: they change how the bytes of an entry are
# interpreted (encryption, patch) or carry key material of the encryption that RFC-9
# prohibits.
NONCOMPLIANT_EXTRA = {
    0x9901: "AE-x (WinZip AES) encryption",
    0x0017: "Strong Encryption Header",
    0x000F: "Patch Descriptor",
    0x0019: "PKCS#7 encryption recipient certificate list",
    0x0021: "policy decryption key record",
    0x0022: "Smartcrypt key provider record",
    0x0023: "Smartcrypt policy key data record",
}

FLAG_ENCRYPTED = 0x0001  # general purpose bit 0
FLAG_STRONG = 0x0040  # bit 6, strong encryption
FLAG_MASKED = 0x2000  # bit 13, masked local header values (central directory encryption)
VERSION_CD_ENCRYPTION = 62  # "version needed to extract" 6.2 = central directory encryption

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
    zip64, version_needed64 = False, None
    j = tail.rfind(b"PK\x06\x07", 0, i)
    if j >= 0:
        zip64 = True
        _, _, rec_off, _ = struct.unpack("<4sLQL", tail[j:j + 20])
        rec = src.read(rec_off, 56)
        (_, _, _, version_needed64, disk64, cd_disk64, _, n64, size64, off64) = struct.unpack("<4sQ2H2L4Q", rec)
        if n_total == 0xFFFF or cd_size == 0xFFFFFFFF or cd_off == 0xFFFFFFFF:
            disk, cd_disk, n_total, cd_size, cd_off = disk64, cd_disk64, n64, size64, off64
    elif n_total == 0xFFFF or cd_size == 0xFFFFFFFF or cd_off == 0xFFFFFFFF:
        raise SystemExit("ZIP64 values in the end record but no ZIP64 end of central directory locator")
    cd = src.read(cd_off, cd_size)
    entries, pos = [], 0
    cd_plain, cd_head = cd[:4] == b"PK\x01\x02", cd[:4]
    while cd_plain and pos + 46 <= len(cd):
        (sig, _, _, flags, method, _, _, _, csize, usize, nlen, elen, clen2,
         dstart, _, _, off) = CD_STRUCT.unpack_from(cd, pos)
        if sig != b"PK\x01\x02":
            cd_plain = False  # malformed after some valid records
            break
        name_bytes = cd[pos + 46:pos + 46 + nlen]
        name = name_bytes.decode("utf-8")
        ex = cd[pos + 46 + nlen:pos + 46 + nlen + elen]
        ids, k, upath_mismatch = [], 0, False
        while k + 4 <= len(ex):
            hid, hsz = struct.unpack_from("<HH", ex, k)
            ids.append(hid)
            if hid == 0x0001 and off == 0xFFFFFFFF:
                p = k + 4 + (8 if usize == 0xFFFFFFFF else 0) + (8 if csize == 0xFFFFFFFF else 0)
                off = struct.unpack_from("<Q", ex, p)[0]
            if hid == 0x7075 and hsz >= 5 and ex[k + 4] == 1:
                crc = struct.unpack_from("<L", ex, k + 5)[0]
                if crc == zlib.crc32(name_bytes) & 0xFFFFFFFF:  # only valid if it matches the header name
                    upath_mismatch = ex[k + 9:k + 4 + hsz] != name_bytes
            k += 4 + hsz
        entries.append(dict(name=name, flags=flags, method=method, extra=ids, disk=dstart, off=off,
                            upath_mismatch=upath_mismatch))
        pos += 46 + nlen + elen + clen2
    return dict(disk=disk, cd_disk=cd_disk, n_total=n_total, zip64=zip64, version_needed64=version_needed64,
                comment=comment, entries=entries, cd_size=cd_size, cd_plain=cd_plain, cd_head=cd_head)


def report(loc, must, should, info, central_directory_read=True):
    print(f"== {loc}")
    for m in must:
        print(f"  MUST violated : {m}")
    for s in should:
        print(f"  SHOULD/notice : {s}")
    for i in info:
        print(f"  info          : {i}")
    if must:
        print("  RESULT        : NOT CONFORMING")
    elif central_directory_read:
        print("  RESULT        : no MUST violations found (central directory only)")
    else:
        print("  RESULT        : central directory could not be checked")
    return 1 if must else 0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit(__doc__)
    loc = args[0]
    src = Source(loc)
    d = read_directory(src)
    E = d["entries"]
    must, should, info = [], [], []

    # Encryption of the central directory
    if d["version_needed64"] == VERSION_CD_ENCRYPTION:
        must.append("ZIP64 end of central directory record needs version 6.2 (central directory encryption)")
    if not d["cd_plain"]:
        why = ("an archive extra data record, as used with central directory encryption"
               if d["cd_head"] == b"PK\x06\x08" else
               "possibly an archive decryption header (encrypted central directory) or a corrupt archive")
        must.append(f"the central directory is not readable as plain central directory records: {why}")
        info.append(f"central directory {d['cd_size']} B, {src.requests} read requests; "
                    "the remaining checks need a readable central directory and were skipped")
        return report(loc, must, should, info, central_directory_read=False)

    names = [e["name"] for e in E]
    if "zarr.json" not in names:
        must.append("root-level zarr.json is missing")
    bad = [e["name"] for e in E if e["method"] != 0]
    if bad:
        must.append(f"{len(bad)} entries are not STORE, e.g. {bad[0]} (method {[e['method'] for e in E if e['method'] != 0][0]})")
    enc = [e["name"] for e in E if e["flags"] & (FLAG_ENCRYPTED | FLAG_STRONG)]
    if enc:
        must.append(f"{len(enc)} encrypted entries, e.g. {enc[0]}")
    masked = [e["name"] for e in E if e["flags"] & FLAG_MASKED]
    if masked:
        must.append(f"{len(masked)} entries have general purpose bit 13 set (masked local header values, "
                    f"central directory encryption), e.g. {masked[0]}")

    noncompliant, unlisted = {}, {}
    for e in E:
        for h in e["extra"]:
            if h in NONCOMPLIANT_EXTRA:
                noncompliant.setdefault(h, e["name"])
            elif h not in COMPLIANT_EXTRA:
                unlisted.setdefault(h, e["name"])
    for h, n in noncompliant.items():
        must.append(f"non-compliant extra field 0x{h:04x} ({NONCOMPLIANT_EXTRA[h]}), first seen on {n}")
    if unlisted:
        first = next(iter(unlisted.items()))
        should.append("extra fields on neither the compliant nor the non-compliant list cannot be verified: "
                      + ", ".join(f"0x{h:04x}" for h in unlisted) + f" (first seen on {first[1]})")
    upath = [e["name"] for e in E if e["upath_mismatch"]]
    if upath:
        should.append(f"{len(upath)} entries have an Info-ZIP Unicode Path extra field (0x7075) that differs from "
                      f"the file name, e.g. {upath[0]}")
    if d["disk"] or d["cd_disk"] or any(e["disk"] for e in E):
        must.append("multi-volume archive")
    first = min((e["off"] for e in E), default=0)
    if first != 0:
        must.append(f"data before the first local file header (lowest offset {first})")
    else:
        head = src.read(0, 30)
        if head[:4] != b"PK\x03\x04":
            must.append("no local file header at offset 0")
        elif len(head) >= 8:
            lflags = struct.unpack_from("<H", head, 6)[0]
            if lflags & (FLAG_ENCRYPTED | FLAG_STRONG):
                must.append("the first local file header has an encryption flag")
            if lflags & FLAG_MASKED:
                must.append("the first local file header has general purpose bit 13 set "
                            "(masked local header values, central directory encryption)")

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
    return report(loc, must, should, info)


if __name__ == "__main__":
    sys.exit(main())
