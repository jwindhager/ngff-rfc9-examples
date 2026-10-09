# Writing and checking OME-Zarr zip files (.ozx): examples and library notes

Material for the RFC-9 implementation guidance (issue #11). Everything marked **tested** was run for
this document (macOS, Python 3.9.6 and 3.12, OpenJDK 21.0.3, zarr-python 3.4.1, Apache Commons
Compress 1.27.1) and checked with `../check_ozx.py`. Libraries not listed here were not tested.

## Where concrete examples come from

[Zipglancer](https://github.com/JaneliaSciComp/zipglancer) (a client-side explorer for ZIP and `.ozx`
archives) is a **reader**, so it provides reader-side examples and test data, not writer examples:

- `src/lib/zipReader.ts`: opening a remote archive with `@zip.js/zip.js` `HttpRangeReader`, reading the
  central directory and the archive comment with range requests (`openZip`).
- `src/lib/ozx.ts`: parsing the RFC-9 archive comment (`ome.version`, `ome.zipFile.centralDirectory.jsonFirst`).
- `src/data/sciVisDatasets.ts`: about 50 public `.ozx` files, e.g.
  `https://ome-zarr-scivis.s3.us-east-1.amazonaws.com/v0.5/96x2-ozx/bunny.ozx`.
  `check_ozx.py` found no MUST violations in the three it was run on (`bunny`, `skull`,
  `marmoset_neurons`): all STORE, no extra fields, `zarr.json` records first, `jsonFirst: true`, no ZIP64.
  (Their writer is not documented in the repository.)

Note: `openZip` calls `reader.getEntries()`, which reads the whole central directory. In the files read,
`jsonFirst` is parsed and displayed, but I saw no early termination of the central directory parse.

## Examples in this directory

| File | Library | Notes |
|---|---|---|
| `write_ozx.py` | Python standard library `zipfile` | STORE, ZIP64 requested, central directory sorted, comment |
| `java/WriteOzxJdk.java` | JDK `java.util.zip` (run with `java WriteOzxJdk.java dir out.ozx`) | STORE, comment, `zarr.json` written first; no ZIP64 |
| `java/WriteOzxCommonsCompress.java` | Apache Commons Compress | STORE, ZIP64 always, comment |
| `write_ozx_stream_zip.py` | `stream-zip` (third party) | STORE, ZIP64 end records and extra fields, comment added by patching |
| `../check_ozx.py` | Python standard library | checks the MUST rules on a path or URL using range requests |

All three writers produced archives that `check_ozx.py` reports as having no MUST violations and that
`unzip -t` accepts.

## Known deficiencies of the libraries tested

"Tested" = observed in this work; "source" = read in the library source.

### Python `zipfile` (3.9.6, 3.12) — tested
1. **No API for the central directory order.** The record order is the order of `ZipFile.filelist`.
   `write_ozx.py` sorts that list before `close()`. This relies on a CPython implementation detail.
2. **ZIP64 can only be requested per entry, and only affects local headers.** `ZipFile.open(zinfo, "w", force_zip64=True)`
   puts a ZIP64 extra field (20 bytes) in each local header, but the central directory records, the
   end record and "version needed to extract" (20) are unchanged for small archives. There is no way to force the
   ZIP64 end-of-central-directory records on an archive below the 4 GiB / 65,535 entry limits.
3. **Duplicate names are allowed.** Writing a name twice emits a `UserWarning: Duplicate name` and produces
   two central directory records. There is no API to remove an entry in the versions tested.
4. The archive comment must be `bytes` (at most 65,535).

### zarr-python 3.4.1 `ZipStore` (Python 3.12) — tested
1. **Duplicate `zarr.json` records** whenever metadata is rewritten (attributes updated twice gave two records for
   `zarr.json` and for `image/zarr.json`). This is the behavior reported in Comment 4.
2. No archive comment, no ZIP64 forcing, no `zarr.json`-first ordering.
3. On the other hand: entries are STORED by default and no extra fields are written, so no MUST is violated.

### JDK `java.util.zip.ZipOutputStream` (OpenJDK 21) — tested
1. **STORED entries need size and CRC-32 before the entry is written**, so the data has to be read twice or buffered.
2. **No way to force ZIP64**; it is used only when needed.
3. **The central directory order is the write order**, so `zarr.json`-first order forces all `zarr.json` entries to be
   written before any chunk (conflicts with writing metadata after the data, see Comment 4).
4. `setComment` works.

### JDK zip file system (`jdk.zipfs`, OpenJDK 21) — tested and source
1. STORED is supported through the documented `compressionMethod=STORED` environment property.
2. **ZIP64 end records can be forced only with the undocumented `forceZIP64End` property** (it works: the end records
   appear), as noted in Review 1.
3. **No archive comment**: the end record is written with comment length 0 (source), so the RFC's recommended comment cannot be written.
4. **Writes extended timestamp extra fields (`0x5455`)** on file entries (allowed by the RFC); I found no option to switch this off.
5. **Creates directory entries** such as `image/` and `image/s0/` when parent directories are created
   (required before writing a file into a directory). The RFC does not say whether directory entries are permitted.
   The RFC ignores directory entries for the purpose of `jsonFirst`.

### Apache Commons Compress 1.27.1 — tested
1. Works for STORED, ZIP64 always and comment (`WriteOzxCommonsCompress.java`); needs `commons-io` and `commons-lang3` at run time.
2. **ZIP64 overhead is not "a few bytes"**: on the 7-entry example the archive is 440 bytes larger than the
   JDK writer's (about 63 bytes per entry): 20 bytes of ZIP64 extra field per local header plus 32 bytes per central
   directory record, among other differences. At one million entries that is tens of megabytes.
3. Central directory order is the write order.

### Common to all writers tested
- **None of them lets the writer set the central directory order independently of the write order**, other than the
  `zipfile` list workaround above.
- **"ZIP64 used" means different things**: Python marks local headers only, Commons Compress marks local headers and
  central directory records, zipfs marks only the end record (with `forceZIP64End`), the JDK output stream only
  when required. A reader checking only the central directory or only the end record will see different things.
- **Per-entry ZIP64 cost is 20 to about 60 bytes**, not "a few bytes" as the RFC text says.

### Reading remote archives in a browser (from zipglancer's source)
- Some servers (zipglancer's source names S3) support range requests but do not expose `Accept-Ranges` through CORS, so zip.js needs
  `forceRangeRequests` and the data server must send `Access-Control-Allow-Headers: Range` and
  `Access-Control-Expose-Headers: Content-Range, Content-Length, Accept-Ranges` (zipglancer README).

### Not tested
zip4j, libzip, Go `archive/zip`, Rust `zip` / `zarrs_zip`, tensorstore, zarrita.js writers, zip.js writers, and
command line tools (`zip`, `7z`). Their behavior regarding STORE, ZIP64, extra fields, ordering and comments should be
checked with `check_ozx.py` before the RFC makes statements about them.

## Open questions this raised for the RFC

1. Directory entries (`image/`): permitted in the RFC text. Tested: zarr-python 3.4.1 reads a hierarchy with and without
   them identically (members, `list_dir`, array data); only the raw `store.list()` additionally returns the
   keys ending in `/`. They are ignored by `jsonFirst` (they may appear anywhere), so writers such as the JDK zip file
   system, which creates them first, can still produce `jsonFirst` archives if the `zarr.json` entries precede the other files.

Resolved in the RFC text: "use ZIP64" is now split into (1) the ZIP64 end of central directory records
SHOULD be present, and (2) entries that are or may become larger than 4 GiB SHOULD use the ZIP64 extra fields
(MAY for all entries), and the per-entry overhead is stated as tens of bytes. Against these recommendations of the examples here:

| Writer | (1) end records | (2) extra fields |
|---|---|---|
| `write_ozx.py` (Python `zipfile`) | cannot be written for small archives | local headers only |
| `WriteOzxJdk.java` | no | no (cannot be forced) |
| `WriteOzxCommonsCompress.java` | yes | yes (all entries) |
| JDK zipfs with `forceZIP64End` | yes | no |

## Additional Python options

### `stream-zip` (third-party, tested with 0.0.84) — tested
`write_ozx_stream_zip.py`. Not based on `zipfile`; writes ZIP and ZIP64 per member.
1. With `NO_COMPRESSION_64` members it writes **both ZIP64 recommendations**: the ZIP64 end records and the ZIP64 extra field in
   every central directory record (the only library tested other than Commons Compress that does).
2. `extended_timestamps=False` avoids the `0x5455` extra field.
3. Central directory order is the member order; STORE is supported (`NO_COMPRESSION_32` / `NO_COMPRESSION_64`).
4. **No archive comment option** (checked in the function signature). `set_zip_comment()` in the example patches the comment
   onto a finished archive by rewriting the last two bytes of the end record; this also works for archives from the JDK zip file system
   (tested: `unzip -t` accepts the result).
5. It also supports AES encryption, which RFC-9 prohibits at the archive level, and directory members.
6. Not investigated: memory behavior for stored members.

### Recent changes in Python's `zipfile`
From the CPython documentation (3.14) and source:
| Version | Change | Relevance |
|---|---|---|
| 3.4 | ZIP64 extensions enabled by default (`allowZip64=True`) | the end records are written only when required |
| 3.5 | writing to unseekable streams | |
| 3.11 | `ZipFile.mkdir()` | directory entries (permitted by RFC-9) |
| 3.13 | `ZipFile.open(..., "w")` file objects have `name` and `mode`; public `compress_level` | |
| 3.14 | `ZIP_ZSTANDARD` compression | **not allowed** in OME-Zarr zip files (STORE only) |
| 3.14 | `ZipFile.writestr` respects `SOURCE_DATE_EPOCH` | reproducible archives |
| 3.15 (rc3) | no `zipfile` additions found in the docs | |
| planned for 3.16 (unreleased; in the development branch, 3.16.0a0) | **`ZipFile.remove()` and `ZipFile.repack()`** | see below |

Tested for this document: `remove()` and `repack()` taken from the CPython `main` branch source and run on 3.15.0rc3. After a metadata
rewrite had produced a duplicate `zarr.json`, `zf.remove(old_info)` followed by `zf.repack([old_info])` left one `zarr.json` (the newer
value), reclaimed the obsolete local entry (101 bytes in the example), and `testzip()` passed. This is the tool needed for the RFC's
recommendation to remove duplicate central directory records after appending. These methods are planned for Python 3.16 and are not yet released; the API is documented as "versionadded: next" and may change before release.
Still true in the development source: no API for the central directory order (sort `ZipFile.filelist`), and the ZIP64 end records
are written only when the 4 GiB / 65,535 entry limits are exceeded (`_write_end_record`), so they cannot be forced for a small archive.

```python
# Planned for Python 3.16 (not yet released): drop an obsolete duplicate record and reclaim its space
with zipfile.ZipFile("data.ozx", "a") as zf:
    old = [i for i in zf.infolist() if i.filename == "zarr.json"][0]  # the first, obsolete record
    zf.repack([zf.remove(old)])
```

Not tested: `pyzipper`, `zipfly`, `remotezip` (reading), `fsspec` zip filesystem.
