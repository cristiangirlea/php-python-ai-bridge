"""Measure index loads and guard cost in the 512m compose mcp service; remove fixtures on exit."""

import gc
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from bridge_mcp import index, tools
from bridge_mcp.protocol import Bridge


def fixture(directory: Path, non_bmp: bool = False) -> None:
    chars, overlap, dimensions = 1000, 200, 384
    text = ('A searchable passage with "quoted" words and a backslash \\.\n' * 20)[:chars]
    if non_bmp:
        text = "\U0001f600" + text[1:]
    encoded = len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2
    target = index.MAX_JSON_TEXT_BYTES // 4
    count = min(index.MAX_INDEX_CHUNKS, target // (encoded + 64)) if non_bmp else index.MAX_INDEX_CHUNKS
    chunks = [{"source": number // 20000, "start": (number % 20000) * (chars - overlap),
               "end": (number % 20000) * (chars - overlap) + chars, "text": text} for number in range(count)]
    row = [1.0] + [0.0] * (dimensions - 1)
    chunking: dict[str, int | str] = {"chars": chars, "overlap": overlap}

    def write() -> None:
        index.write_index(directory, model="hashing-bow-not-a-model", backend="lexical", dimensions=dimensions,
                          chunking=chunking, sources=["a.txt", "b.txt", "c.txt"], chunks=chunks,
                          rows=(row for _ in range(count)))

    write()
    if non_bmp:
        gap = target - (directory / index.SIDECAR).stat().st_size
        chunking["padding"] = "a" * (gap - len(', "padding": ""'))
        write()
        assert (directory / index.SIDECAR).stat().st_size == target
    print(json.dumps({"fixture": directory.name, "chunks": count, "dimensions": dimensions,
                      "non_bmp_in_every_chunk": non_bmp, "text_characters_per_chunk": chars,
                      "escaped_text_bytes_per_chunk": encoded,
                      "sidecar_bytes": (directory / index.SIDECAR).stat().st_size,
                      "vector_bytes": (directory / index.VECTORS).stat().st_size}), flush=True)


def guard_cost() -> None:
    data = b'{"model":"' + b"\\n" * ((index.MAX_SIDECAR_BYTES - 12) // 2) + b'"}'
    old_tokens = re.compile(rb'["\\\[\]{},:]')
    started = time.monotonic()
    quoted = False
    escaped_until = containers = tokens = depth = iterations = 0
    for match in old_tokens.finditer(data):
        iterations += 1
        if match.start() < escaped_until:
            continue
        token = match[0]
        if quoted:
            if token == b"\\":
                escaped_until = match.start() + 2
            elif token == b'"':
                quoted = False
            continue
        if token == b'"':
            quoted = True
        elif token in (b"[", b"{"):
            containers += 1
            depth += 1
            tokens += 1
        elif token in (b"]", b"}"):
            depth -= 1
        elif token in (b",", b":"):
            tokens += 1
        index._check(containers <= index.MAX_JSON_CONTAINERS and tokens <= index.MAX_JSON_TOKENS
                     and depth <= index.MAX_JSON_DEPTH, "JSON complexity limit")
    old_seconds = time.monotonic() - started
    started = time.monotonic()
    index._guard_json(data)
    new_seconds = time.monotonic() - started
    print(json.dumps({"scan_bytes": len(data), "old_iterations": iterations, "new_iterations": 5,
                      "old_seconds": old_seconds, "new_seconds": new_seconds,
                      "speedup": old_seconds / new_seconds}), flush=True)
    assert old_seconds / new_seconds >= 5


def main() -> None:
    cgroup = Path("/sys/fs/cgroup")
    limit = int((cgroup / "memory.max").read_text())
    assert limit == 512 * 1024 * 1024, limit
    if sys.argv[1] == "scan":
        guard_cost()
        return
    scenario = sys.argv[2]
    assert scenario in ("ascii", "ascii-pair", "unicode-pair"), scenario
    names = ("first",) if scenario == "ascii" else ("first", "second")
    with tempfile.TemporaryDirectory(prefix="index-memory-", dir=sys.argv[1]) as temporary:
        directory = Path(temporary)
        for name in names:
            fixture(directory / name, non_bmp=scenario == "unicode-pair" and name == "first")
        gc.collect()
        server = tools.build_server(Bridge("http://mcp-worker:8090", os.environ["BRIDGE_TOKEN"]),
                                    backend="lexical", root=directory, timeout_ms=60000)
        cache = index.IndexCache()
        for number, name in enumerate(names, 1):
            sidecar = directory / name / index.SIDECAR
            assert len(cache._items) == (0 if number == 1 else 1)
            loaded = cache.get(sidecar)
            resident_bytes = loaded.resident_bytes
            assert cache.get(sidecar) is loaded
            assert len(cache._items) == 1
            del loaded
            gc.collect()
            peak = int((cgroup / "memory.peak").read_text())
            print(json.dumps({"scenario": scenario, "loads": number, "memory_peak_bytes": peak,
                              "memory_peak_mib": peak / 1024**2, "cached_indexes": len(cache._items),
                              "loaded_object_bytes": resident_bytes,
                              "cached_file_bytes": sum(item.size for item in cache._items.values()),
                              "cached_object_bytes": sum(item.resident_bytes for item in cache._items.values())}),
                  flush=True)
            assert peak <= 440 * 1024**2, (peak, limit)
        del server, cache


if __name__ == "__main__":
    main()
