"""Measure index loads and guard cost in the 512m compose mcp service; remove fixtures on exit."""

import asyncio
import gc
import json
import os
import re
import sys
import tempfile
import threading
import time
import weakref
from pathlib import Path
from unittest.mock import patch

from mcp import Client

from bridge_mcp import index, tools
from bridge_mcp.protocol import Bridge


def fixture(directory: Path, non_bmp: bool = False, wide: bool = False) -> None:
    chars, overlap, dimensions = 4000 if wide else 1000, 200, 384
    text = ('A searchable passage with "quoted" words and a backslash \\.\n' * 80)[:chars]
    if non_bmp:
        text = "\U0001f600" + text[1:]
    elif wide:
        text = "\u0100" + text[1:]
    encoded = len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2
    target = min(index.MAX_SIDECAR_BYTES, index.MAX_JSON_TEXT_BYTES // (4 if non_bmp else 2 if wide else 1))
    count = min(index.MAX_INDEX_CHUNKS, (target - 1024) // (encoded + 80)) if non_bmp or wide else index.MAX_INDEX_CHUNKS
    per_source = (index.MAX_SOURCE_BYTES - chars) // (chars - overlap) + 1
    chunks = [{"source": number // per_source, "start": (number % per_source) * (chars - overlap),
               "end": (number % per_source) * (chars - overlap) + chars, "text": text} for number in range(count)]
    row = [1.0] + [0.0] * (dimensions - 1)
    chunking: dict[str, int | str] = {"chars": chars, "overlap": overlap}

    def write() -> None:
        index.write_index(directory, model="hashing-bow-not-a-model", backend="lexical", dimensions=dimensions,
                          chunking=chunking, sources=[f"{number}.txt" for number in range((count - 1) // per_source + 1)], chunks=chunks,
                          rows=(row for _ in range(count)))

    write()
    if non_bmp or wide:
        gap = target - (directory / index.SIDECAR).stat().st_size
        chunking["padding"] = "a" * (gap - len(', "padding": ""'))
        write()
        assert (directory / index.SIDECAR).stat().st_size == target
    print(json.dumps({"fixture": directory.name, "chunks": count, "dimensions": dimensions,
                      "non_bmp_in_every_chunk": non_bmp, "text_characters_per_chunk": chars,
                      "width_two_in_every_chunk": wide,
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


async def concurrent_searches(directory: Path, cgroup: Path) -> None:
    bridge = Bridge("http://mcp-worker:8090", os.environ["BRIDGE_TOKEN"])
    server = tools.build_server(bridge, backend="lexical", root=directory, timeout_ms=300000)
    entered = {task: threading.Event() for task in ("embed", "rerank")}
    release = {task: threading.Event() for task in entered}
    counts = dict.fromkeys(entered, 0)
    lock = threading.Lock()
    references = []
    original_load = index.load_index

    def load(*args, **kwargs):
        loaded = original_load(*args, **kwargs)
        references.append(weakref.ref(loaded))
        assert sum(reference() is not None for reference in references) == 1
        return loaded

    def run(task, payload, *args):
        with lock:
            counts[task] += 1
            if counts[task] == 3:
                entered[task].set()
        assert release[task].wait(240), f"{task} searches did not overlap"
        if task == "embed":
            return {"model": "hashing-bow-not-a-model", "dimensions": 384,
                    "vectors": [[1.0] + [0.0] * 383]}
        return {"model": "lexical-demo-not-a-model",
                "rankings": [{"index": number, "score": 1.0} for number in range(payload["top_k"])]}

    async def search(name):
        async with Client(server, raise_exceptions=True) as client:
            result = await client.call_tool("bridge_search", {"query": "passage", "index_path": name})
            assert not result.is_error, result.content
            assert result.structured_content["chunks"] == 50000
            assert len(result.structured_content["results"]) == 5

    async def wait_for_jobs(task):
        while not entered[task].is_set():
            await asyncio.sleep(0.01)

    with patch.object(index, "load_index", load), patch.object(bridge, "run", run):
        calls = [asyncio.create_task(search(name)) for name in ("first", "second", "third")]
        try:
            for task in entered:
                await asyncio.wait_for(wait_for_jobs(task), 240)
                alive = sum(reference() is not None for reference in references)
                peak = int((cgroup / "memory.peak").read_text())
                print(json.dumps({"scenario": "search-triple", "waiting_on": task, "jobs_in_flight": counts[task],
                                  "live_indexes": alive, "memory_peak_bytes": peak,
                                  "memory_peak_mib": peak / 1024**2}), flush=True)
                assert alive == 1
                assert peak <= 440 * 1024**2, peak
                release[task].set()
        finally:
            for event in release.values():
                event.set()
            await asyncio.gather(*calls)
    peak = int((cgroup / "memory.peak").read_text())
    print(json.dumps({"scenario": "search-triple", "completed": 3, "memory_peak_bytes": peak,
                      "memory_peak_mib": peak / 1024**2}), flush=True)
    assert peak <= 440 * 1024**2, peak


def main() -> None:
    cgroup = Path("/sys/fs/cgroup")
    limit = int((cgroup / "memory.max").read_text())
    assert limit == 512 * 1024 * 1024, limit
    if sys.argv[1] == "scan":
        guard_cost()
        return
    scenario = sys.argv[2]
    assert scenario in ("ascii", "ascii-pair", "unicode-pair", "ascii-unicode-pair", "unicode-wide-pair", "search-triple"), scenario
    names = (("first", "second", "third") if scenario == "search-triple"
             else ("first",) if scenario == "ascii" else ("first", "second"))
    with tempfile.TemporaryDirectory(prefix="index-memory-", dir=sys.argv[1]) as temporary:
        directory = Path(temporary)
        for name in names:
            fixture(directory / name,
                    non_bmp=(scenario in ("unicode-pair", "unicode-wide-pair") and name == "first")
                    or (scenario == "ascii-unicode-pair" and name == "second"),
                    wide=scenario == "unicode-wide-pair" and name == "second")
        gc.collect()
        if scenario == "search-triple":
            asyncio.run(concurrent_searches(directory, cgroup))
            return
        server = tools.build_server(Bridge("http://mcp-worker:8090", os.environ["BRIDGE_TOKEN"]),
                                    backend="lexical", root=directory, timeout_ms=60000)
        cache = index.IndexCache()
        for number, name in enumerate(names, 1):
            sidecar = directory / name / index.SIDECAR
            assert len(cache._items) == (0 if number == 1 else 1)
            loaded = cache.get(sidecar)
            assert cache.get(sidecar) is loaded
            assert len(cache._items) == 1
            del loaded
            gc.collect()
            peak = int((cgroup / "memory.peak").read_text())
            print(json.dumps({"scenario": scenario, "loads": number, "memory_peak_bytes": peak,
                              "memory_peak_mib": peak / 1024**2, "cached_indexes": len(cache._items),
                              "cached_file_bytes": sum(item.size for item in cache._items.values())}),
                  flush=True)
            assert peak <= 440 * 1024**2, (peak, limit)
        del server, cache


if __name__ == "__main__":
    main()
