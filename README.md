# RFC-9 supporting scripts and examples

Scripts, examples and library notes accompanying
[RFC-9: OME-Zarr zip files](https://ngff.openmicroscopy.org/rfc/9/).
Extracted from `scripts/rfc9/` in [jwindhager/ngff](https://github.com/jwindhager/ngff)
(branch `mkitti/easy-items`), with its commit history.

## Contents

| Path | Contents |
|---|---|
| [`checker/check_ozx.py`](checker/check_ozx.py) | checks an `.ozx` file (local path or URL, range requests only) against the MUST / SHOULD rules of RFC-9 |
| [`writers/python/`](writers/python) | writers using the standard library `zipfile` (`write_ozx.py`) and `stream-zip` (`write_ozx_stream_zip.py`) |
| [`writers/java/`](writers/java) | writers using the JDK `java.util.zip` (`WriteOzxJdk.java`) and Apache Commons Compress (`WriteOzxCommonsCompress.java`) |
| [`performance/`](performance/README.md) | central directory benchmark and a check of the external performance figures cited in the RFC |
| [`library-notes.md`](library-notes.md) | what each library tested can and cannot do with respect to RFC-9 |

## Usage

    python3 writers/python/write_ozx.py path/to/image.ome.zarr out.ozx
    java writers/java/WriteOzxJdk.java path/to/image.ome.zarr out.ozx
    python3 checker/check_ozx.py out.ozx

`check_ozx.py`, `write_ozx.py` and the performance scripts need only the Python standard library.
`write_ozx_stream_zip.py` needs `stream-zip`; `WriteOzxCommonsCompress.java` needs Apache Commons Compress
(see the comment at the top of each file).
