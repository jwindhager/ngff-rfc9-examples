#!/usr/bin/env python3
"""Check the external performance claims cited in RFC-9 against their sources.

The RFC "Performance" section summarizes two external sources. This script
fetches them and reports, for every claimed figure, whether it appears in the
source text, with surrounding context so a human can confirm the meaning.

Usage: python3 verify_external_claims.py [--cache-dir DIR]
Only the Python standard library is used.
"""
import argparse
import json
import re
import sys
import urllib.request

README = "https://raw.githubusercontent.com/hamk-uas/datacube-storage-lab/main/README.md"
ISSUE = "https://api.github.com/repos/csaybar/ESA-zar-zip-decision/issues/6"

# (source key, description, regex, how the RFC uses it)
CLAIMS = [
    ("readme", "S3 zipped Zarr (async) read time ~7.1 s", r"7\.08", "7.1 s"),
    ("readme", "S3 directory Zarr read time ~7.7 s", r"7\.67", "7.7 s"),
    ("readme", "NVMe zipped Zarr (async) read time ~1.3 s", r"1\.34", "1.3 s"),
    ("readme", "NVMe directory Zarr read time ~1.2 s", r"1\.19", "1.2 s"),
    ("readme", "Sync ZipStore on S3 ~24 s", r"23\.9", "24 s"),
    ("readme", "Directory Zarr file count 8,562", r"8[ ,]?562", "8,562 files"),
    ("readme", "Directory Zarr (20,40,80) copy from /scratch: 30 min", r"Zarr 20, 40, 80\*\|8562\|192\|30\|Copy\|/scratch", "27-30 min"),
    ("readme", "Directory Zarr (20,40,80) copy from Allas S3: 27 min", r"same as above\|\|\|27\|Copy\|Allas S3", "27-30 min"),
    ("readme", "Zipped Zarr copy from /scratch: 10 min", r"Zipped Zarr 20, 40, 80\*\|1\|192\|10\|Copy\|/scratch", "10-12 min"),
    ("readme", "Zipped Zarr copy from Allas S3: 12 min", r"same as above\|\|\|12\|Copy\|Allas S3", "10-12 min"),
    ("readme", "Sync ZipStore run baseline: S3 Zarr 7.53 s, NVMe zipped 3.21 s", r"Allas S3 Zarr: 7\.53.*NVMe zipped Zarr: 3\.21", "(context for 24 s)"),
    ("issue", "'not slower than' claim", r"not slower than", "ESA report"),
]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "rfc9-verify"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def context(text, m, width=160):
    a, b = max(0, m.start() - width), min(len(text), m.end() + width)
    return " ".join(text[a:b].split())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", help="optional directory to save fetched sources")
    args = ap.parse_args()

    sources = {"readme": fetch(README), "issue": json.loads(fetch(ISSUE)).get("body", "")}
    if args.cache_dir:
        for k, v in sources.items():
            with open(f"{args.cache_dir}/{k}.txt", "w") as f:
                f.write(v)

    missing = 0
    for src, desc, rx, rfc in CLAIMS:
        ms = list(re.finditer(rx, sources[src], flags=re.I))
        status = "FOUND  " if ms else "MISSING"
        missing += not ms
        print(f"[{status}] {desc}  (RFC says: {rfc}; source: {src}; {len(ms)} hit(s))")
        for m in ms[:2]:
            print(f"    ...{context(sources[src], m)}...")
    print(f"\n{len(CLAIMS) - missing}/{len(CLAIMS)} claims have a matching string in the source.")
    print("A match shows the number appears, not that the RFC's reading of it is right: read the context.")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
