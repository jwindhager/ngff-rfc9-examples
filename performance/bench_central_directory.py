#!/usr/bin/env python3
"""Measure ZIP central directory costs for OME-Zarr zip files (RFC-9).

Builds synthetic OME-Zarr-like ZIP archives with N entries that follow the
RFC-9 recommendations (STORE, ZIP64, root zarr.json first, other zarr.json
files next in breadth-first order = "jsonFirst", then chunk/shard entries),
and measures:

  1. zipfile_open   stdlib zipfile.ZipFile(path): reads and parses the entire
                    central directory (what a naive reader does).
  2. read_cd_bytes  reading only the tail (EOCD [+ZIP64 locator/record]) and
                    the central directory bytes, as range requests would.
  3. parse_full     parsing all N central directory records (names+offsets).
  4. index_hash     building a dict name -> offset (O(N) expected).
  5. index_sorted   sorting names for bisect lookup (O(N log N)).
  6. lookup_hash / lookup_bisect  per-lookup cost after indexing.
  7. parse_jsonfirst  jsonFirst discovery: parse records from the start of the
                    central directory and stop at the first entry that is neither a
                    zarr.json nor a directory entry (cost depends on the number of zarr.json
                    entries J, not on N).

Only the central directory is touched; entry data is tiny filler. Timings are
CPU/page-cache timings on a local file; they do NOT include network latency.
For remote storage the relevant quantity is the number of bytes and requests,
so the script also reports the bytes a range-request reader would fetch.

Usage:
  python3 bench_central_directory.py [--sizes 100,1000,10000,100000]
                                     [--arrays 5] [--reps 5] [--json OUT.json]
                                     [--dir DIR]
Standard library only.

Expected complexity (N = number of entries, J = number of zarr.json entries,
n = mean name length):
  locate EOCD / ZIP64 records   O(1)      bounded tail read (<= 64 KiB + 22 B)
  central directory size        O(N n)    ~ 46 B + name + extra per entry
  parse all records             O(N)
  hash index                    O(N) build, O(1) expected lookup
  sorted index                  O(N log N) build, O(log N) lookup
  jsonFirst discovery           O(J) records (independent of N)
  hierarchy discovery, no jsonFirst:  O(N) (all records must be read)
"""
import argparse
import bisect
import json
import math
import os
import random
import statistics
import struct
import sys
import tempfile
import time
import zipfile

EOCD_SIG = b"PK\x05\x06"
EOCD64_LOC_SIG = b"PK\x06\x07"
EOCD64_SIG = b"PK\x06\x06"
CD_SIG = b"PK\x01\x02"
CD_STRUCT = struct.Struct("<4s6H3L5H2L")  # 46 bytes
assert CD_STRUCT.size == 46


# ---------------------------------------------------------------- building --

def entry_names(n_entries, n_arrays):
    """Names in RFC-9 recommended order: root zarr.json, then the other
    zarr.json files (breadth-first), then chunk/shard entries."""
    names = ["zarr.json"]
    for a in range(n_arrays):
        names.append(f"s{a}/zarr.json")
    n_chunks = max(0, n_entries - len(names))
    per = max(1, math.ceil(n_chunks / n_arrays))
    chunk_names = []
    for a in range(n_arrays):
        for i in range(per):
            if len(chunk_names) >= n_chunks:
                break
            chunk_names.append(f"s{a}/c/0/{i // 1000}/{i % 1000}")
    return names + chunk_names


def build_archive(path, n_entries, n_arrays, order="jsonfirst"):
    names = entry_names(n_entries, n_arrays)
    if order == "jsonlast":  # zarr.json files at the end (not jsonFirst)
        names = [x for x in names if not x.endswith("zarr.json")] + \
                [x for x in names if x.endswith("zarr.json")]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for name in names:
            zi = zipfile.ZipInfo(name, date_time=(2025, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_STORED
            with zf.open(zi, "w", force_zip64=True) as f:  # ZIP64 for every entry
                f.write(b"{}" if name.endswith("zarr.json") else b"\0" * 8)
    return len(names)


# ----------------------------------------------------------------- parsing --

def read_tail_info(f, size):
    """Return (n_entries, cd_size, cd_offset, tail_bytes_read) using only a
    bounded read from the end of the file."""
    tail_len = min(size, 65536 + 22 + 20 + 56)
    f.seek(size - tail_len)
    tail = f.read(tail_len)
    i = tail.rfind(EOCD_SIG)
    if i < 0:
        raise ValueError("EOCD not found")
    (_, _, _, _, n_total, cd_size, cd_off, _) = struct.unpack("<4s4H2LH", tail[i:i + 22])
    if n_total == 0xFFFF or cd_size == 0xFFFFFFFF or cd_off == 0xFFFFFFFF:
        j = tail.rfind(EOCD64_LOC_SIG, 0, i)
        if j < 0:
            raise ValueError("ZIP64 locator not found")
        _, _, rec_off, _ = struct.unpack("<4sLQL", tail[j:j + 20])
        f.seek(rec_off)
        rec = f.read(56)
        (sig, _, _, _, _, _, n_total, _, cd_size, cd_off) = struct.unpack("<4sQ2H2L4Q", rec)
        assert sig == EOCD64_SIG
        tail_len += 56
    return n_total, cd_size, cd_off, tail_len


def iter_cd(buf, limit=None):
    """Yield (name, local_header_offset) for each central directory record."""
    pos, end = 0, len(buf)
    count = 0
    while pos + 46 <= end and (limit is None or count < limit):
        (sig, _, _, _, _, _, _, _, csize, usize, nlen, elen, clen,
         _, _, _, off) = CD_STRUCT.unpack_from(buf, pos)
        if sig != CD_SIG:
            raise ValueError(f"bad CD signature at {pos}")
        name = buf[pos + 46:pos + 46 + nlen].decode("utf-8")
        if off == 0xFFFFFFFF:  # offset lives in the ZIP64 extra field
            ex = buf[pos + 46 + nlen:pos + 46 + nlen + elen]
            k = 0
            while k + 4 <= len(ex):
                hid, hsz = struct.unpack_from("<HH", ex, k)
                if hid == 0x0001:
                    vals = []
                    p = k + 4
                    # fields present only if the 32-bit counterpart is 0xFFFFFFFF
                    if usize == 0xFFFFFFFF:
                        vals.append(struct.unpack_from("<Q", ex, p)[0]); p += 8
                    if csize == 0xFFFFFFFF:
                        vals.append(struct.unpack_from("<Q", ex, p)[0]); p += 8
                    off = struct.unpack_from("<Q", ex, p)[0]
                    break
                k += 4 + hsz
        yield name, off
        pos += 46 + nlen + elen + clen
        count += 1


def parse_jsonfirst(f, cd_off, cd_size, block=65536):
    """Read the central directory in blocks from its start, stopping at the
    first non-zarr.json entry. Returns (names, bytes_read)."""
    f.seek(cd_off)
    buf = b""
    read = 0
    names = []
    pos = 0
    while True:
        want = min(block, cd_size - read)
        if want <= 0 and pos >= len(buf):
            break
        if want > 0:
            buf += f.read(want)
            read += want
        while pos + 46 <= len(buf):
            nlen, elen, clen = struct.unpack_from("<3H", buf, pos + 28)
            rec = 46 + nlen + elen + clen
            if pos + rec > len(buf):
                break
            name = buf[pos + 46:pos + 46 + nlen].decode("utf-8")
            if name.endswith("/"):  # directory entries are ignored by jsonFirst
                pos += rec
                continue
            if not name.endswith("zarr.json"):
                return names, read
            names.append(name)
            pos += rec
        if want <= 0:
            break
    return names, read


# ------------------------------------------------------------------ timing --

def timeit(fn, reps):
    ts = []
    out = None
    for _ in range(reps):
        t = time.perf_counter()
        out = fn()
        ts.append(time.perf_counter() - t)
    return statistics.median(ts), out


def loglog_slope(xs, ys):
    lx = [math.log(x) for x in xs]
    ly = [math.log(y) for y in ys]
    mx, my = sum(lx) / len(lx), sum(ly) / len(ly)
    num = sum((a - mx) * (b - my) for a, b in zip(lx, ly))
    den = sum((a - mx) ** 2 for a in lx)
    return num / den if den else float("nan")


def bench_one(path, n_entries, n_arrays, reps, order):
    n = build_archive(path, n_entries, n_arrays, order)
    size = os.path.getsize(path)
    row = {"N": n, "file_bytes": size, "order": order}

    row["zipfile_open_s"], zf = timeit(lambda: zipfile.ZipFile(path), reps)
    assert len(zf.namelist()) == n
    zf.close()

    with open(path, "rb") as f:
        n_cd, cd_size, cd_off, tail_bytes = read_tail_info(f, size)
        assert n_cd == n, (n_cd, n)

        def read_cd():
            f.seek(cd_off)
            return f.read(cd_size)
        row["read_cd_bytes_s"], buf = timeit(read_cd, reps)
        row["cd_bytes"] = cd_size
        row["tail_bytes"] = tail_bytes

        row["parse_full_s"], recs = timeit(lambda: list(iter_cd(buf)), reps)
        assert len(recs) == n
        row["index_hash_s"], idx = timeit(lambda: {k: v for k, v in recs}, reps)
        # sort a shuffled copy: generated names are nearly sorted already, which
        # would make timsort look O(N); an arbitrary central directory is not
        shuffled = recs[:]
        random.Random(0).shuffle(shuffled)
        row["index_sorted_s"], srt = timeit(lambda: sorted(shuffled), reps)
        keys = [k for k, _ in srt]

        probe = [recs[(i * 7919) % n][0] for i in range(1000)]

        def look_hash():
            for p in probe:
                idx[p]
        t, _ = timeit(look_hash, reps)
        row["lookup_hash_us"] = t / len(probe) * 1e6

        def look_bisect():
            for p in probe:
                keys[bisect.bisect_left(keys, p)]
        t, _ = timeit(look_bisect, reps)
        row["lookup_bisect_us"] = t / len(probe) * 1e6

        t, (jnames, jbytes) = timeit(lambda: parse_jsonfirst(f, cd_off, cd_size), reps)
        row["parse_jsonfirst_s"] = t
        row["jsonfirst_found"] = len(jnames)
        row["jsonfirst_bytes_read"] = jbytes
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="100,1000,10000,100000")
    ap.add_argument("--arrays", type=int, default=5, help="number of arrays (scale levels)")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--json", help="write raw results to this file")
    ap.add_argument("--dir", help="directory for temporary archives")
    args = ap.parse_args()
    sizes = [int(x) for x in args.sizes.split(",")]

    rows = []
    with tempfile.TemporaryDirectory(dir=args.dir) as d:
        for order in ("jsonfirst", "jsonlast"):
            for s in sizes:
                path = os.path.join(d, f"bench_{order}_{s}.ozx")
                r = bench_one(path, s, args.arrays, args.reps, order)
                rows.append(r)
                os.remove(path)
                print(f"done N={r['N']} order={order}", file=sys.stderr)

    hdr = ("N", "order", "file MB", "CD MB", "zipfile_open ms", "parse_full ms",
           "hash idx ms", "sort idx ms", "lookup hash us", "lookup bisect us",
           "jsonFirst ms", "jsonFirst KB read", "zarr.json found")
    print("\n" + " | ".join(hdr))
    print("-" * 150)
    for r in rows:
        print(" | ".join(str(x) for x in (
            r["N"], r["order"], f"{r['file_bytes'] / 1e6:.2f}", f"{r['cd_bytes'] / 1e6:.2f}",
            f"{r['zipfile_open_s'] * 1e3:.2f}", f"{r['parse_full_s'] * 1e3:.2f}",
            f"{r['index_hash_s'] * 1e3:.2f}", f"{r['index_sorted_s'] * 1e3:.2f}",
            f"{r['lookup_hash_us']:.2f}", f"{r['lookup_bisect_us']:.2f}",
            f"{r['parse_jsonfirst_s'] * 1e3:.3f}", f"{r['jsonfirst_bytes_read'] / 1e3:.1f}",
            r["jsonfirst_found"])))

    jf = [r for r in rows if r["order"] == "jsonfirst"]
    if len(jf) >= 3:
        xs = [r["N"] for r in jf]
        print("\nEmpirical scaling exponent k in time ~ N^k (log-log least squares; "
              "theory: 1 for O(N), ~1.0-1.1 for O(N log N), 0 for O(1)/O(J)):")
        for key, label in (("zipfile_open_s", "zipfile.ZipFile open (full CD parse)"),
                           ("parse_full_s", "own parser, full CD"),
                           ("index_hash_s", "hash index build"),
                           ("index_sorted_s", "sorted index build"),
                           ("lookup_hash_us", "hash lookup (per query)"),
                           ("lookup_bisect_us", "bisect lookup (per query)"),
                           ("parse_jsonfirst_s", "jsonFirst discovery"),
                           ("cd_bytes", "central directory size (bytes)")):
            print(f"  {label:42s} k = {loglog_slope(xs, [r[key] for r in jf]):6.2f}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"python": sys.version, "platform": sys.platform, "rows": rows}, f, indent=1)


if __name__ == "__main__":
    main()
