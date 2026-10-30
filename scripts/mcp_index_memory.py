"""Measure two full index loads in the compose mcp service; remove the fixtures on exit."""

import gc
import json
import os
import sys
import tempfile
from pathlib import Path

from bridge_mcp import index, tools
from bridge_mcp.protocol import Bridge


def fixture(directory: Path) -> None:
    count, dimensions = index.MAX_INDEX_CHUNKS, 384
    chars, overlap = 1000, 200
    text = ('A searchable passage with "quoted" words and a backslash \\.\n' * 20)[:chars]
    chunks = [{"source": number // 20000, "start": (number % 20000) * (chars - overlap),
               "end": (number % 20000) * (chars - overlap) + chars, "text": text} for number in range(count)]
    row = [1.0] + [0.0] * (dimensions - 1)
    index.write_index(directory, model="hashing-bow-not-a-model", backend="lexical", dimensions=dimensions,
                      chunking={"chars": chars, "overlap": overlap}, sources=["a.txt", "b.txt", "c.txt"], chunks=chunks,
                      rows=(row for _ in range(count)))
    print(json.dumps({"fixture": directory.name, "chunks": count, "dimensions": dimensions,
                      "text_characters_per_chunk": chars,
                      "escaped_text_bytes_per_chunk": len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2,
                      "sidecar_bytes": (directory / index.SIDECAR).stat().st_size,
                      "vector_bytes": (directory / index.VECTORS).stat().st_size}), flush=True)


def main() -> None:
    cgroup = Path("/sys/fs/cgroup")
    limit = int((cgroup / "memory.max").read_text())
    assert limit == 512 * 1024 * 1024, limit
    with tempfile.TemporaryDirectory(prefix="index-memory-", dir=sys.argv[1]) as temporary:
        directory = Path(temporary)
        for name in ("first", "second"):
            fixture(directory / name)
        gc.collect()
        server = tools.build_server(Bridge("http://mcp-worker:8090", os.environ["BRIDGE_TOKEN"]),
                                    backend="lexical", root=directory, timeout_ms=60000)
        cache = index.IndexCache()
        for number, name in enumerate(("first", "second"), 1):
            sidecar = directory / name / index.SIDECAR
            loaded = cache.get(sidecar)
            assert loaded.count == 50000 and loaded.dimensions == 384
            resident_bytes = loaded.resident_bytes
            if cache._items:
                assert cache.get(sidecar) is loaded
            del loaded
            gc.collect()
            peak = int((cgroup / "memory.peak").read_text())
            print(json.dumps({"loads": number, "memory_peak_bytes": peak, "memory_peak_mib": peak / 1024**2,
                              "cached_indexes": len(cache._items),
                              "loaded_object_bytes": resident_bytes,
                              "cached_file_bytes": sum(item.size for item in cache._items.values()),
                              "cached_object_bytes": sum(item.resident_bytes for item in cache._items.values())}),
                  flush=True)
            assert peak <= 440 * 1024**2, (peak, limit)
        del server, cache


if __name__ == "__main__":
    main()
