"""Build a searchable index over the bridge protocol, and load and search one. Standard library only.

    python -m bridge_mcp.index build <files or directories...> --out DIR [--chunk-chars 1000] [--overlap-chars 200]

The builder splits UTF-8 text files into overlapping character windows, embeds them through the worker's
allowlisted `embed` task in batches of 32, and writes two files into DIR:

- index.json: format id, embedding model and backend, dimensions, chunking, source paths relative to DIR, and
  every chunk's source, code-point offsets and text;
- vectors.f32: count x dimensions little-endian float32, row-major, whose SHA-256 index.json records.

This module never imports the MCP SDK: it is the script that talks to the HTTP protocol directly.
"""

import argparse
import array
import hashlib
import heapq
import json
import math
import os
import re
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from .protocol import Bridge, BridgeError

FORMAT = "bridge-index/1"
SIDECAR = "index.json"
VECTORS = "vectors.f32"
MAX_INDEX_CHUNKS = 50000
MAX_SOURCE_BYTES = 16 * 1024 * 1024
# Parsing a sidecar costs several times its size in memory, and the mcp container has 512 MiB.
MAX_SIDECAR_BYTES = 64 * 1024 * 1024
CACHE_BYTES = 128 * 1024 * 1024
MAX_CHUNK_CHARACTERS = 8192
# The worker embeds at most 32 texts per job, and the encoded request must stay under 262144 bytes.
EMBED_BATCH = 32
BATCH_BYTES = 200000
UNIT_TOLERANCE = 1e-3
CAPACITY_WAIT_S = 5
CAPACITY_RETRIES = 60
DEFAULT_SUFFIXES = (".txt", ".md")
_WORD = re.compile(r"\S")
_SPACE = re.compile(r"\s")


class InvalidIndex(ValueError):
    """An index on disk is malformed, inconsistent or outside where it may be read. Messages are ours."""


class BuildError(Exception):
    """A build was refused; nothing was written."""


@dataclass
class LoadedIndex:
    model: str
    backend: str
    dimensions: int
    count: int
    chunking: dict
    sources: list
    chunks: list
    vectors: array.array
    directory: Path
    vectors_path: Path = field(repr=False)
    # (device, inode, mtime, size) of the sidecar and vectors files exactly as read.
    stamps: tuple = field(repr=False, default=())

    @property
    def size(self) -> int:
        return sum(stamp[3] for stamp in self.stamps)


# ------------------------------------------------------------------------------------------------ chunking

def _skip_space(text: str, i: int) -> int:
    word = _WORD.search(text, i)
    return word.start() if word else len(text)


def _last_space(text: str, low: int, high: int):
    """The index of the last whitespace character in text[low:high + 1], or None. Unicode whitespace counts,
    as it does everywhere else in the chunker."""
    last = None
    for match in _SPACE.finditer(text, low, high + 1):
        last = match.start()
    return last


def _next_word(text: str, i: int) -> int:
    """The first word start at or after i; inside a word, the start of the following word."""
    n = len(text)
    if 0 < i < n and not text[i - 1].isspace() and not text[i].isspace():
        space = _SPACE.search(text, i)
        if space is None:
            return n
        i = space.start()
    return _skip_space(text, i)


def chunk_text(text: str, chars: int = 1000, overlap: int = 200) -> list:
    """Character windows of at most `chars`, cut at whitespace where one exists in the second half of the
    window, each starting on a word and overlapping the previous one by about `overlap` characters.

    Every non-whitespace character lands in at least one chunk. Offsets are code points of `text`.
    """
    if type(chars) is not int or not 200 <= chars <= 4000:
        raise ValueError("chunk size must be 200-4000 characters")
    if type(overlap) is not int or not 0 <= overlap < chars // 2:
        raise ValueError("overlap must be at least 0 and less than half the chunk size")
    n = len(text)
    spans = []
    start = _skip_space(text, 0)
    while start < n:
        limit = start + chars
        if limit >= n:
            end = n
        else:
            cut = _last_space(text, start + chars // 2, limit)
            end = cut if cut is not None else limit
        while end > start and text[end - 1].isspace():
            end -= 1
        spans.append((start, end))
        if limit >= n:
            break
        following = _next_word(text, end - overlap)
        if not start < following <= end:
            # No word starts inside the overlap: continue hard, mid-word, so nothing is skipped.
            following = end - overlap if end - overlap > start else end
        start = _skip_space(text, following)
    return spans


# ------------------------------------------------------------------------------------------------ format

def _check(condition: bool, message: str) -> None:
    if not condition:
        raise InvalidIndex(message)


def write_index(out: Path, *, model: str, backend: str, dimensions: int, chunking: dict, sources: list,
                chunks: list, rows) -> dict:
    """Stream `rows` into vectors.f32, then write index.json. Nothing is replaced unless both were written; the
    two renames are separate, so a reader between them sees a checksum mismatch and must retry."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    vectors_tmp, sidecar_tmp = out / (VECTORS + ".tmp"), out / (SIDECAR + ".tmp")
    digest, count = hashlib.sha256(), 0
    try:
        with vectors_tmp.open("wb") as output:
            for row in rows:
                values = array.array("f", row)
                if len(values) != dimensions:
                    raise BuildError(f"a vector has {len(values)} components, expected {dimensions}")
                if sys.byteorder == "big":
                    values.byteswap()
                data = values.tobytes()
                digest.update(data)
                output.write(data)
                count += 1
        if count != len(chunks):
            raise BuildError(f"{count} vectors for {len(chunks)} chunks")
        meta = {"format": FORMAT, "model": model, "backend": backend, "dimensions": dimensions, "count": count,
                "chunking": chunking, "vectors": {"file": VECTORS, "dtype": "float32", "byte_order": "little",
                                                  "sha256": digest.hexdigest()},
                "sources": sources, "chunks": chunks}
        sidecar_tmp.write_text(json.dumps(meta, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        os.replace(vectors_tmp, out / VECTORS)
        os.replace(sidecar_tmp, out / SIDECAR)
        return meta
    finally:
        for leftover in (vectors_tmp, sidecar_tmp):
            leftover.unlink(missing_ok=True)


def load_index(sidecar: Path, within: Path | None = None) -> LoadedIndex:
    """Read and verify an index. With `within`, its vectors file must also resolve inside that directory."""
    sidecar = Path(sidecar)
    try:
        with sidecar.open("rb") as handle:
            sidecar_stamp = _fstamp(handle)
            _check(sidecar_stamp[3] <= MAX_SIDECAR_BYTES, f"{SIDECAR} is larger than "
                   f"{MAX_SIDECAR_BYTES // (1024 * 1024)} MiB")
            meta = json.loads(handle.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        if isinstance(error, InvalidIndex):
            raise
        raise InvalidIndex(f"{SIDECAR} cannot be read as JSON ({type(error).__name__})") from None
    _check(isinstance(meta, dict) and meta.get("format") == FORMAT, f"not a {FORMAT} index")
    model, backend, dimensions, count = meta.get("model"), meta.get("backend"), meta.get("dimensions"), meta.get("count")
    _check(isinstance(model, str) and model != "", "the index names no model")
    _check(isinstance(backend, str) and backend != "", "the index names no backend")
    _check(type(dimensions) is int and dimensions >= 1, "dimensions must be a positive integer")
    _check(type(count) is int and 1 <= count <= MAX_INDEX_CHUNKS, f"an index holds 1-{MAX_INDEX_CHUNKS} chunks")
    sources, chunks, chunking = meta.get("sources"), meta.get("chunks"), meta.get("chunking")
    _check(isinstance(chunking, dict), "chunking must be an object")
    _check(isinstance(sources, list) and all(isinstance(item, str) and item for item in sources),
           "sources must be a list of paths")
    _check(isinstance(chunks, list) and len(chunks) == count, "the chunk list does not match the count")
    for chunk in chunks:
        _check(isinstance(chunk, dict) and type(chunk.get("source")) is int and 0 <= chunk["source"] < len(sources)
               and type(chunk.get("start")) is int and type(chunk.get("end")) is int
               and 0 <= chunk["start"] < chunk["end"] and isinstance(chunk.get("text"), str)
               and 0 < len(chunk["text"]) <= MAX_CHUNK_CHARACTERS, "a chunk is malformed")
    described = meta.get("vectors")
    _check(isinstance(described, dict) and described.get("dtype") == "float32"
           and described.get("byte_order") == "little", "vectors must be little-endian float32")
    name = described.get("file")
    _check(isinstance(name, str) and name not in ("", ".", "..") and Path(name).name == name and "\\" not in name,
           "the vectors file must be a bare file name beside index.json")
    vectors_path = (sidecar.parent / name).resolve()
    if within is not None:
        _check(vectors_path.is_relative_to(Path(within).resolve()), "the vectors file is outside the allowed directory")
    expected = count * dimensions * 4
    try:
        with vectors_path.open("rb") as handle:
            vectors_stamp = _fstamp(handle)
            _check(vectors_stamp[3] == expected, f"{name} does not hold {count} x {dimensions} float32 values")
            data = handle.read()
    except OSError as error:
        raise InvalidIndex(f"{name} cannot be read ({type(error).__name__})") from None
    _check(len(data) == expected, f"{name} changed while it was read")
    _check(hashlib.sha256(data).hexdigest() == described.get("sha256"), f"{name} does not match its SHA-256")
    vectors = array.array("f")
    vectors.frombytes(data)
    if sys.byteorder == "big":
        vectors.byteswap()
    rows = memoryview(vectors)
    for row in range(count):
        values = rows[row * dimensions:(row + 1) * dimensions]
        _check(all(math.isfinite(value) for value in values)
               and abs(math.sqrt(math.sumprod(values, values)) - 1.0) <= UNIT_TOLERANCE,
               f"vector {row} is not a finite unit vector")
    return LoadedIndex(model, backend, dimensions, count, chunking, sources, chunks, vectors,
                       sidecar.parent, vectors_path, (sidecar_stamp, vectors_stamp))


def search(loaded: LoadedIndex, query_vector, n: int) -> list:
    """The n chunks with the highest dot product, as (chunk, score); ties keep the lower chunk number."""
    # A memoryview slices rows without copying them.
    width, vectors = loaded.dimensions, memoryview(loaded.vectors)
    query = array.array("f", query_vector)
    scores = ((math.sumprod(query, vectors[row * width:(row + 1) * width]), -row) for row in range(loaded.count))
    return [(-negative, score) for score, negative in heapq.nlargest(n, scores)]


def _identity(status) -> tuple:
    # Every rebuild replaces both files by rename, so the inode changes even when size and time do not.
    return status.st_dev, status.st_ino, status.st_mtime_ns, status.st_size


def _fstamp(handle) -> tuple:
    return _identity(os.fstat(handle.fileno()))


def _stamp(path: Path) -> tuple:
    return _identity(path.stat())


class IndexCache:
    """Loaded indexes keyed by resolved sidecar path, bounded by the bytes of their files, and reloaded when
    either file is replaced or changes. Loading happens outside the lock, so a hit never waits for a load."""

    def __init__(self, limit_bytes: int = CACHE_BYTES):
        self.limit_bytes = limit_bytes
        self._items = OrderedDict()
        self._lock = threading.Lock()

    def get(self, sidecar: Path, within: Path | None = None) -> LoadedIndex:
        sidecar = Path(sidecar).resolve()
        key = (sidecar, within)
        with self._lock:
            loaded = self._items.get(key)
        if loaded is not None:
            try:
                if loaded.stamps == (_stamp(sidecar), _stamp(loaded.vectors_path)):
                    with self._lock:
                        if key in self._items:
                            self._items.move_to_end(key)
                    return loaded
            except OSError:
                pass
        try:
            loaded = load_index(sidecar, within)
        except InvalidIndex:
            # A rebuild replaces the two files one after the other; a load between the renames sees a
            # checksum mismatch. One retry after a pause separates that from a genuinely broken index.
            _pause(0.2)
            loaded = load_index(sidecar, within)
        with self._lock:
            self._items[key] = loaded
            self._items.move_to_end(key)
            while len(self._items) > 1 and sum(item.size for item in self._items.values()) > self.limit_bytes:
                self._items.popitem(last=False)
        return loaded


# ------------------------------------------------------------------------------------------------ building

def _pause(seconds: float) -> None:
    time.sleep(seconds)


def collect(inputs: list, suffixes=DEFAULT_SUFFIXES) -> list:
    """Every file named, and every file with one of `suffixes` under every directory named, sorted."""
    files = []
    for item in inputs:
        path = Path(item)
        if path.is_dir():
            files.extend(sorted(child for child in path.rglob("*") if child.is_file() and child.suffix in suffixes))
        elif path.is_file():
            files.append(path)
        else:
            raise BuildError(f"no such file or directory: {item}")
    unique = list(dict.fromkeys(file.resolve() for file in files))
    if not unique:
        raise BuildError(f"no {' or '.join(suffixes)} files to index")
    return unique


def _batches(texts: list):
    batch, size = [], 0
    for number, text in enumerate(texts):
        encoded = len(json.dumps(text, ensure_ascii=False).encode("utf-8")) + 1
        if batch and (len(batch) == EMBED_BATCH or size + encoded > BATCH_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(number)
        size += encoded
    if batch:
        yield batch


def _embed(bridge, texts: list, timeout_s: float, identity: dict) -> list:
    for attempt in range(CAPACITY_RETRIES + 1):
        try:
            result = bridge.run("embed", {"texts": texts}, timeout_s)
            break
        except BridgeError as error:
            if error.code != "capacity_exceeded" or attempt == CAPACITY_RETRIES:
                raise BuildError(f"the worker refused an embed job: {error.code}") from None
            _pause(CAPACITY_WAIT_S)
    model, dimensions, vectors = result.get("model"), result.get("dimensions"), result.get("vectors")
    if identity and (model, dimensions) != (identity["model"], identity["dimensions"]):
        raise BuildError(f"the worker changed model mid-build: {identity['model']!r} then {model!r}")
    identity.setdefault("model", model)
    identity.setdefault("dimensions", dimensions)
    if (not isinstance(model, str) or not model or type(dimensions) is not int or dimensions < 1
            or not isinstance(vectors, list) or len(vectors) != len(texts)):
        raise BuildError("the worker returned an invalid embedding result")
    for vector in vectors:
        if (not isinstance(vector, list) or len(vector) != dimensions
                or not all(type(value) in (int, float) and math.isfinite(value) for value in vector)
                or abs(math.sqrt(math.sumprod(vector, vector)) - 1.0) > UNIT_TOLERANCE):
            raise BuildError("the worker returned a vector that is not a finite unit vector")
    return vectors


def build(bridge, inputs: list, out: Path, chars: int = 1000, overlap: int = 200,
          suffixes=DEFAULT_SUFFIXES, timeout_s: float = 60.0, log=None) -> dict:
    """Chunk every input file, embed the chunks through the worker, and write the index into `out`."""
    out = Path(out)
    if out.exists() and not out.is_dir():
        raise BuildError(f"--out is an existing file: {out}")
    files = collect(inputs, suffixes)
    backend = bridge.health()["backend"]
    sources, chunks = [], []
    for number, path in enumerate(files):
        if path.stat().st_size > MAX_SOURCE_BYTES:
            raise BuildError(f"{path} is larger than {MAX_SOURCE_BYTES // (1024 * 1024)} MiB")
        try:
            # newline="" keeps the file's own line endings, so offsets are code points of the file as stored.
            with path.open(encoding="utf-8", newline="") as handle:
                text = handle.read()
        except UnicodeDecodeError:
            raise BuildError(f"{path} is not UTF-8 text") from None
        sources.append(Path(os.path.relpath(path, out.resolve())).as_posix())
        chunks.extend({"source": number, "start": start, "end": end, "text": text[start:end]}
                      for start, end in chunk_text(text, chars, overlap))
    if not chunks:
        raise BuildError("the input files hold no text")
    if len(chunks) > MAX_INDEX_CHUNKS:
        raise BuildError(f"{len(chunks)} chunks exceed the {MAX_INDEX_CHUNKS}-chunk limit; index fewer files")
    texts = [chunk["text"] for chunk in chunks]
    identity = {}

    def rows():
        done = 0
        for batch in _batches(texts):
            yield from _embed(bridge, [texts[number] for number in batch], timeout_s, identity)
            done += len(batch)
            if log:
                log(f"embedded {done}/{len(texts)} chunks")

    # The first batch fixes the model and dimensions, so it runs before the file is opened.
    stream = rows()
    first = next(stream)
    meta = write_index(out, model=identity["model"], backend=backend, dimensions=identity["dimensions"],
                       chunking={"chars": chars, "overlap": overlap}, sources=sources, chunks=chunks,
                       rows=_prepend(first, stream))
    return meta


def _prepend(first, rest):
    yield first
    yield from rest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bridge_mcp.index")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("build", help="build an index from UTF-8 text files")
    command.add_argument("inputs", nargs="+", help="files, or directories searched for --suffix files")
    command.add_argument("--out", required=True, help="the directory to write index.json and vectors.f32 into")
    command.add_argument("--chunk-chars", type=int, default=1000)
    command.add_argument("--overlap-chars", type=int, default=200)
    command.add_argument("--suffix", default=",".join(DEFAULT_SUFFIXES), help="comma-separated, for directories")
    command.add_argument("--timeout-s", type=float, default=60.0)
    arguments = parser.parse_args(argv)

    def say(message: str) -> None:
        print(f"bridge_mcp.index: {message}", file=sys.stderr)

    try:
        chunk_text("", arguments.chunk_chars, arguments.overlap_chars)
        if not 1 <= arguments.timeout_s <= 300:
            raise ValueError("--timeout-s must be between 1 and 300 seconds")
        bridge = Bridge(os.environ.get("BRIDGE_URL", "http://127.0.0.1:8090"), os.environ.get("BRIDGE_TOKEN", ""))
        suffixes = tuple(item.strip() for item in arguments.suffix.split(",") if item.strip())
        meta = build(bridge, arguments.inputs, Path(arguments.out), arguments.chunk_chars, arguments.overlap_chars,
                     suffixes, arguments.timeout_s, say)
    except (ValueError, BuildError, BridgeError) as error:
        say(str(error))
        return 2
    say(f"wrote {meta['count']} chunks from {len(meta['sources'])} files to {arguments.out} with {meta['model']!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
