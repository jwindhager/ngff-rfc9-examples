# RFC-9 supporting scripts

Standard-library Python only (tested with 3.9).

## `verify_external_claims.py`

Fetches the two external sources cited in the RFC's _Performance_ section and checks that each
figure quoted in the RFC appears in the source, printing context so that the interpretation can be
confirmed by a human. A match shows only that the number occurs, not that the RFC reads it correctly.

    python3 scripts/rfc9/verify_external_claims.py

## `bench_central_directory.py`

Builds synthetic OME-Zarr-like zip files (STORE, ZIP64 on every entry, `zarr.json` files first as in
`jsonFirst: true`, or last) and measures central directory costs.
Timings are CPU / page-cache timings on a local file and do not include network latency.

    python3 scripts/rfc9/bench_central_directory.py --sizes 100,1000,10000,100000,1000000 --reps 3

### Expected complexity

N = number of ZIP entries, J = number of `zarr.json` entries (groups + arrays), n = mean name length.

| Operation | Cost | Notes |
|---|---|---|
| Locate EOCD (+ ZIP64 locator/record) | O(1) | one bounded range read from the end (<= 64 KiB + 22 B) |
| Central directory size | O(N·n) | 46 B per record + name + extra field |
| Parse all records | O(N) | |
| Hash index (name → offset) | O(N) build, O(1) expected lookup | |
| Sorted index | O(N log N) build, O(log N) lookup | |
| Hierarchy discovery with `jsonFirst: true` | O(J) records | stops at the first non-`zarr.json` entry; independent of N |
| Hierarchy discovery without `jsonFirst` | O(N) | every record must be read to be sure no `zarr.json` is missed |
| Reading one chunk/shard after indexing | 1 range request (+1 for the local file header if the extra-field length is unknown) | |

Sharding reduces N (roughly to the number of shards plus metadata entries), which reduces every O(N) term.

### Limitations

- Synthetic data: tiny entries, a handful of arrays, short names. Real archives have longer names
  (larger central directory) and more `zarr.json` entries.
- Python implementation: absolute times depend on the language. Scaling behavior, bytes per entry
  and the `jsonFirst` vs. full-parse contrast are the transferable results.
- Local file, warm page cache: no network latency. For remote access the relevant quantities are the
  number of requests and bytes (central directory size is reported).
