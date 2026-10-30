"""The index builder and loader: chunking, the on-disk format, building over the protocol, search and caching."""

import array
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import weakref
from pathlib import Path
from unittest.mock import MagicMock, patch

from ai_bridge.jobs import Settings
from ai_bridge.server import BridgeServer
from ai_bridge.tasks import execute
from bridge_mcp import index
from bridge_mcp.protocol import Bridge, BridgeError

TOKEN = "test-only-bridge-token-never-use-in-production"


def worker_vector(text):
    """What the in-process hashing worker returns for one text."""
    return execute("embed", {"texts": [text]}, "lexical", "", lambda *_: None)["vectors"][0]


class ChunkTests(unittest.TestCase):
    def test_chunks_are_bounded_overlapping_snapped_and_cover_every_word(self):
        text = " ".join(f"w{i:03d}" for i in range(600))
        spans = index.chunk_text(text, 1000, 200)
        self.assertGreater(len(spans), 3)
        covered = set()
        for number, (start, end) in enumerate(spans):
            with self.subTest(chunk=number):
                self.assertLessEqual(end - start, 1000)
                self.assertTrue(text[start:end].strip())
                self.assertFalse(text[start].isspace() or text[end - 1].isspace())
                self.assertTrue(start == 0 or text[start - 1].isspace(), "chunks start on a word")
                self.assertTrue(end == len(text) or text[end].isspace(), "chunks end on a word")
                covered.update(range(start, end))
        for (start, end), (following, _) in zip(spans, spans[1:]):
            self.assertLess(start, following)
            self.assertLessEqual(following, end, "consecutive chunks overlap or touch")
            self.assertLessEqual(end - following, 200 + 5, "overlap is at most the requested amount plus a word")
        self.assertTrue(all(i in covered for i, char in enumerate(text) if not char.isspace()))

    def test_short_blank_and_unbroken_texts(self):
        self.assertEqual(index.chunk_text("  hello world  ", 1000, 200), [(2, 13)])
        self.assertEqual(index.chunk_text("x" * 199, 1000, 200), [(0, 199)])
        self.assertEqual(index.chunk_text(" \n\t ", 1000, 200), [])
        # A 2500-character token has no word boundary: cuts are hard, overlapping, and still cover it all.
        spans = index.chunk_text("y" * 2500, 1000, 200)
        self.assertEqual(spans[0], (0, 1000))
        self.assertEqual(spans[-1][1], 2500)
        self.assertTrue(all(following <= end for (_, end), (following, _) in zip(spans, spans[1:])))

    def test_unicode_whitespace_is_a_cut_point(self):
        text = "\u3000".join(f"word{i}" for i in range(400))
        spans = index.chunk_text(text, 1000, 200)
        self.assertGreater(len(spans), 2)
        for _start, end in spans[:-1]:
            self.assertTrue(text[end].isspace(), "an ideographic space is a word boundary")

    def test_parameters_are_bounded(self):
        for chars, overlap in [(199, 0), (4001, 0), (1000, 500), (1000, -1)]:
            with self.subTest(chars=chars, overlap=overlap), self.assertRaises(ValueError):
                index.chunk_text("text", chars, overlap)


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name) / "index"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, rows, **overrides):
        options = dict(model="m", backend="lexical", dimensions=3, chunking={"chars": 1000, "overlap": 200},
                       sources=["a.txt"], chunks=[{"source": 0, "start": 0, "end": 1, "text": f"t{i}"}
                                                  for i in range(len(rows))], rows=rows)
        options.update(overrides)
        index.write_index(self.out, **options)
        return self.out / index.SIDECAR

    def test_round_trip_preserves_everything(self):
        sidecar = self.write([[1.0, 0.0, 0.0], [0.0, 0.6, 0.8], [0.0, 0.0, 1.0]])
        loaded = index.load_index(sidecar)
        self.assertEqual((loaded.model, loaded.backend, loaded.dimensions, loaded.count), ("m", "lexical", 3, 3))
        self.assertEqual(loaded.sources, ["a.txt"])
        self.assertEqual([chunk["text"] for chunk in loaded.chunks], ["t0", "t1", "t2"])
        self.assertIsInstance(loaded.vectors, array.array)
        for got, want in zip(loaded.vectors, [1, 0, 0, 0, 0.6, 0.8, 0, 0, 1]):
            self.assertAlmostEqual(got, want, places=6)
        self.assertEqual((self.out / index.VECTORS).stat().st_size, 3 * 3 * 4)
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(meta["format"], "bridge-index/1")
        self.assertEqual(meta["vectors"]["sha256"], hashlib.sha256((self.out / index.VECTORS).read_bytes()).hexdigest())
        self.assertFalse(list(self.out.glob("*.tmp")), "temporary files are replaced, not left behind")

    def corrupt(self, change):
        sidecar = self.write([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
        change(sidecar)
        with self.assertRaises(index.InvalidIndex):
            index.load_index(sidecar)

    def edit(self, sidecar, **fields):
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        for key, value in fields.items():
            if key == "vectors_file":
                meta["vectors"]["file"] = value
            else:
                meta[key] = value
        sidecar.write_text(json.dumps(meta), encoding="utf-8")

    def test_every_corruption_is_refused(self):
        def vectors(sidecar):
            return sidecar.parent / index.VECTORS

        self.corrupt(lambda s: self.edit(s, format="bridge-index/2"))
        self.corrupt(lambda s: self.edit(s, model=""))
        self.corrupt(lambda s: self.edit(s, dimensions=4))
        self.corrupt(lambda s: self.edit(s, count=index.MAX_INDEX_CHUNKS + 1))
        self.corrupt(lambda s: self.edit(s, vectors_file="../x"))
        self.corrupt(lambda s: self.edit(s, vectors_file="/etc/hostname"))
        self.corrupt(lambda s: vectors(s).write_bytes(vectors(s).read_bytes()[:-4]))
        # The last byte of the last value (0.0) flips from 0x00 to 0xff: same size, different content.
        self.corrupt(lambda s: vectors(s).write_bytes(vectors(s).read_bytes()[:-1] + b"\xff"))
        self.corrupt(lambda s: s.write_text("{not json", encoding="utf-8"))
        self.corrupt(lambda s: self.edit(s, chunks=[{"source": 3, "start": 0, "end": 1, "text": "t"}] * 2))
        self.corrupt(lambda s: self.edit(s, chunks=[{"source": 0, "start": 2, "end": 1, "text": "t"}] * 2))

    def test_rows_must_be_unit_length(self):
        sidecar = self.write([[2.0, 0.0, 0.0]])
        with self.assertRaises(index.InvalidIndex):
            index.load_index(sidecar)

    def test_oversized_dimensions_are_refused_before_opening_vectors(self):
        for dimensions in (4097, 2147483648):
            with self.subTest(dimensions=dimensions):
                sidecar = self.write([[1.0, 0.0, 0.0]])
                self.edit(sidecar, dimensions=dimensions)
                (self.out / index.VECTORS).unlink()
                with self.assertRaisesRegex(index.InvalidIndex, "dimensions"):
                    index.load_index(sidecar)

    def test_oversized_vector_bytes_are_refused_before_opening_vectors(self):
        sidecar = self.write([[1.0, 0.0, 0.0]])
        chunk = {"source": 0, "start": 0, "end": 1, "text": "t"}
        count = index.MAX_VECTOR_BYTES // (4096 * 4) + 1
        self.edit(sidecar, dimensions=4096, count=count, chunks=[chunk] * count)
        (self.out / index.VECTORS).unlink()
        with self.assertRaisesRegex(index.InvalidIndex, "vector bytes"):
            index.load_index(sidecar)

    def test_statically_oversized_vectors_are_never_read(self):
        sidecar = self.write([[1.0, 0.0, 0.0]])
        with (self.out / index.VECTORS).open("wb") as handle:
            handle.truncate(8 * 1024**3)
        original = os.fdopen
        handles = []

        def opened(fd, *args, **kwargs):
            handle = original(fd, *args, **kwargs)
            wrapped = MagicMock(wraps=handle)
            wrapped.__enter__.return_value = wrapped
            wrapped.__exit__.side_effect = lambda *_: handle.close()
            handles.append(wrapped)
            return wrapped

        with patch.object(os, "fdopen", opened), self.assertRaises(index.InvalidIndex):
            index.load_index(sidecar)
        self.assertEqual(len(handles), 2)
        handles[-1].read.assert_not_called()
        handles[-1].readinto.assert_not_called()

    def test_sidecar_expansion_is_guarded_before_json_parsing(self):
        sidecar = self.write([[1.0, 0.0, 0.0]])
        for payload in ('{"x":[' + '[],' * 50010 + '[]]}',
                        '{"x":[' + '0,' * 600130 + '0]}'):
            with self.subTest(kind=payload[6:8]):
                payload = '{"padding":"' + 'x' * (16 * 1024 * 1024) + '",' + payload[1:]
                self.assertLess(len(payload), index.MAX_SIDECAR_BYTES)
                sidecar.write_text(payload, encoding="utf-8")
                with patch.object(index.json, "loads", wraps=json.loads) as parse:
                    started = time.monotonic()
                    with self.assertRaisesRegex(index.InvalidIndex, "complexity"):
                        index.load_index(sidecar)
                    self.assertLess(time.monotonic() - started, 3)
                parse.assert_not_called()

    def test_deep_sidecar_is_invalid_instead_of_recursion_error(self):
        sidecar = self.write([[1.0, 0.0, 0.0]])
        sidecar.write_text('[' * 2000 + '0' + ']' * 2000, encoding="utf-8")
        started = time.monotonic()
        with self.assertRaisesRegex(index.InvalidIndex, "complexity"):
            index.load_index(sidecar)
        self.assertLess(time.monotonic() - started, 3)

    def test_decoder_recursion_error_is_an_invalid_index(self):
        sidecar = self.write([[1.0, 0.0, 0.0]])
        with patch.object(index.json, "loads", side_effect=RecursionError):
            with self.assertRaises(index.InvalidIndex):
                index.load_index(sidecar)

    def test_wide_sidecars_are_refused_before_decoding_or_parsing(self):
        class Undecodable(bytes):
            def decode(self, *args, **kwargs):
                raise AssertionError("the width guard must run before decoding")

        sidecar = self.write([[1.0, 0.0, 0.0]])
        for marker, width in ((b"\xf0\x9f\x98\x80", 4), (b"\\ud83d\\ude00", 4),
                              (b"\xe4\xb8\xad", 2), (b"\xc4\x80", 2), (b"\\u0100", 2)):
            with self.subTest(marker=marker):
                payload = b'{"model":"' + marker + b'a' * 1024 + b'"}'
                handle = MagicMock()
                handle.__enter__.return_value = handle
                handle.read.return_value = Undecodable(payload)
                with patch.object(index, "MAX_JSON_TEXT_BYTES", len(payload) * width - 1, create=True), \
                     patch.object(index, "_open_regular", return_value=handle), \
                     patch.object(index, "_fstamp", return_value=(0, 0, 0, len(payload))), \
                     patch.object(index.json, "loads") as parse:
                    with self.assertRaisesRegex(index.InvalidIndex, "text"):
                        index.load_index(sidecar)
                    parse.assert_not_called()

    def test_non_bmp_sidecar_exactly_at_the_width_budget_loads(self):
        sidecar = self.write([[1.0, 0.0, 0.0]], model="m\U0001f600")
        budget = 2 * index.MAX_SIDECAR_BYTES // 4
        with sidecar.open("ab") as handle:
            handle.write(b" " * (budget - sidecar.stat().st_size))
        self.assertEqual(sidecar.stat().st_size * 4, 2 * index.MAX_SIDECAR_BYTES)
        self.assertEqual(index.load_index(sidecar).model, "m\U0001f600")

    def test_writer_enforces_the_same_width_budget_without_replacing_files(self):
        sidecar = self.write([[1.0, 0.0, 0.0]])
        before = sidecar.read_bytes()
        with patch.object(index, "MAX_JSON_TEXT_BYTES", 1024, create=True):
            with self.assertRaisesRegex(index.BuildError, "outside the Basic Multilingual Plane.*text"):
                self.write([[0.0, 1.0, 0.0]], model="m\U0001f600" + "a" * 1024)
        self.assertEqual(sidecar.read_bytes(), before)
        self.assertFalse(list(self.out.glob("*.tmp")))

    def test_escape_heavy_sidecar_is_one_match_per_string(self):
        payload = b'{"model":"' + b"\\n" * ((index.MAX_SIDECAR_BYTES - 12) // 2) + b'"}'
        self.assertGreaterEqual(len(payload), index.MAX_SIDECAR_BYTES - 2)
        original = index._JSON_TOKENS
        iterations = 0

        def matches(data):
            nonlocal iterations
            for match in original.finditer(data):
                iterations += 1
                self.assertLessEqual(iterations, 5, "scan a whole JSON string in C")
                yield match

        with patch.object(index, "_JSON_TOKENS") as scanner:
            scanner.finditer.side_effect = matches
            index._guard_json(payload)
        self.assertEqual(iterations, 5)

    def test_cache_validation_failure_is_not_retried(self):
        sidecar = self.write([[1.0, 0.0, 0.0]])
        with patch.object(index, "load_index", side_effect=index.InvalidIndex("bad text")) as load, \
             patch.object(index, "_pause") as pause:
            with self.assertRaises(index.InvalidIndex):
                index.IndexCache().get(sidecar)
        self.assertEqual(load.call_count, 1)
        pause.assert_not_called()

    def test_cache_resident_budget_keeps_one_default_index(self):
        self.assertEqual(index.CACHE_RESIDENT_BYTES, 192 * 1024**2)
        self.assertEqual(index.CACHE_BYTES, 160 * 1024**2)

    def test_cache_evicts_under_object_budget_with_two_real_entries(self):
        sidecar = self.write([[1.0, 0.0, 0.0]])
        other = Path(self.temp.name) / "other"
        index.write_index(other, model="m", backend="lexical", dimensions=3, chunking={},
                          sources=["a.txt"], chunks=[{"source": 0, "start": 0, "end": 1, "text": "t"}],
                          rows=[[1.0, 0.0, 0.0]])
        first = index.load_index(sidecar)
        second = index.load_index(other / index.SIDECAR)
        self.assertLess(first.size + second.size, index.CACHE_BYTES)
        with patch.object(index, "CACHE_RESIDENT_BYTES", max(first.resident_bytes, second.resident_bytes)):
            cache = index.IndexCache()
            cache.get(sidecar)
            self.assertEqual(len(cache._items), 1)
            cache.get(other / index.SIDECAR)
            self.assertEqual(list(cache._items), [(other.joinpath(index.SIDECAR).resolve(), None)])

    def test_cache_hit_proceeds_while_another_key_loads(self):
        sidecar = self.write([[1.0, 0.0, 0.0]])
        cache = index.IndexCache()
        first = cache.get(sidecar)
        entered, release, hit = threading.Event(), threading.Event(), threading.Event()
        errors = []

        def load(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError("blocked loader was not released")
            return first

        def get(path, is_hit=False):
            try:
                self.assertIs(cache.get(path), first)
                if is_hit:
                    hit.set()
            except Exception as error:
                errors.append(error)

        with patch.object(index, "load_index", load):
            loading = threading.Thread(target=get, args=(self.out / "other.json",))
            hitting = threading.Thread(target=get, args=(sidecar, True))
            loading.start()
            try:
                self.assertTrue(entered.wait(1))
                hitting.start()
                self.assertTrue(hit.wait(1), "a cached key must not wait for another load")
            finally:
                release.set()
                loading.join(4)
                if hitting.ident is not None:
                    hitting.join(4)
        self.assertFalse(loading.is_alive() or hitting.is_alive())
        self.assertEqual(errors, [])

    def test_writer_refuses_an_oversized_sidecar_without_replacing_files(self):
        sidecar = self.write([[1.0, 0.0, 0.0]])
        before = sidecar.read_bytes()
        with patch.object(index, "MAX_SIDECAR_BYTES", 1):
            with self.assertRaisesRegex(index.BuildError, "1-byte sidecar limit.*--chunk-chars"):
                self.write([[0.0, 1.0, 0.0]])
        self.assertEqual(sidecar.read_bytes(), before)
        self.assertEqual(list(index.load_index(sidecar).vectors), [1.0, 0.0, 0.0])

    def test_json_guard_ignores_punctuation_and_escaped_quotes_in_text(self):
        text = '[{,:\\"' * 1000
        sidecar = self.write([[1.0, 0.0, 0.0]],
                             chunks=[{"source": 0, "start": 0, "end": len(text), "text": text}])
        self.assertEqual(index.load_index(sidecar).chunks[0]["text"], text)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires FIFOs")
    def test_nonregular_files_are_refused_without_blocking(self):
        for filename in (index.SIDECAR, index.VECTORS):
            with self.subTest(filename=filename):
                sidecar = self.write([[1.0, 0.0, 0.0]])
                path = self.out / filename
                path.unlink()
                os.mkfifo(path)
                errors = []

                def load(sidecar, errors):
                    try:
                        index.load_index(sidecar)
                    except Exception as error:
                        errors.append(error)

                thread = threading.Thread(target=load, args=(sidecar, errors), daemon=True)
                thread.start()
                thread.join(1)
                blocked = thread.is_alive()
                if blocked:
                    # Release a regressed blocking opener so the test leaves no thread behind.
                    fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
                    os.close(fd)
                    thread.join(1)
                self.assertFalse(blocked, "opening a FIFO must not block")
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], index.InvalidIndex)
                path.unlink()

    def test_growing_sidecar_read_is_bounded(self):
        sidecar = self.write([[1.0, 0.0, 0.0]])
        original = os.fdopen
        reads = []

        def opened(fd, *args, **kwargs):
            handle = original(fd, *args, **kwargs)
            wrapped = MagicMock(wraps=handle)
            wrapped.__enter__.return_value = wrapped
            wrapped.__exit__.side_effect = lambda *_: handle.close()

            def read(size=-1):
                reads.append(size)
                with sidecar.open("ab") as growing:
                    growing.write(b" " * 1024)
                return handle.read(size)

            wrapped.read.side_effect = read
            return wrapped

        with patch.object(index, "MAX_SIDECAR_BYTES", sidecar.stat().st_size), patch.object(os, "fdopen", opened):
            with self.assertRaisesRegex(index.InvalidIndex, "larger"):
                index.load_index(sidecar)
        self.assertEqual(reads, [sidecar.stat().st_size - 1024 + 1])

    def test_full_chunk_count_loads_and_stays_cached(self):
        count = 50000
        self.assertEqual(index.MAX_VECTOR_BYTES, count * 384 * 4)
        self.assertGreaterEqual(index.CACHE_BYTES, index.MAX_SIDECAR_BYTES + index.MAX_VECTOR_BYTES)
        text = ('A searchable passage with "quoted" words and a backslash \\.\n' * 20)[:1000]
        chunks = [{"source": number // 20000, "start": (number % 20000) * 800,
                   "end": (number % 20000) * 800 + 1000, "text": text} for number in range(count)]
        # Three dimensions leave room for default-sized escaped text in the test container's 64m tmpfs.
        sidecar = self.write([[1.0, 0.0, 0.0]] * count, chunks=chunks, sources=["a.txt", "b.txt", "c.txt"])
        cache = index.IndexCache()
        with patch.object(index, "load_index", wraps=index.load_index) as load:
            first = cache.get(sidecar)
            self.assertEqual(first.count, count)
            self.assertIs(cache.get(sidecar), first)
            self.assertEqual(load.call_count, 1)

    def test_cache_accounts_for_parsed_objects_as_well_as_file_bytes(self):
        sidecar = self.write([[1.0, 0.0, 0.0]])
        with patch.object(index, "CACHE_RESIDENT_BYTES", 1, create=True):
            cache = index.IndexCache()
            cache.get(sidecar)
            self.assertFalse(cache._items)

    def test_cache_drops_a_stale_key_after_an_over_budget_rebuild(self):
        sidecar = self.write([[1.0, 0.0, 0.0]])
        cache = index.IndexCache(limit_bytes=sidecar.stat().st_size + 12)
        cache.get(sidecar)
        self.write([[1.0, 0.0, 0.0]] * 2)
        cache.get(sidecar)
        self.assertNotIn((sidecar.resolve(), None), cache._items)

    def test_cache_serializes_concurrent_loads(self):
        sidecar = self.write([[1.0, 0.0, 0.0]])
        cache = index.IndexCache()
        original = index.load_index
        entered, release = threading.Event(), threading.Event()
        overlap = []
        active = 0
        errors = []

        def load(*args):
            nonlocal active
            active += 1
            overlap.append(active)
            entered.set()
            release.wait(2)
            try:
                return original(*args)
            finally:
                active -= 1

        def get(within):
            try:
                cache.get(sidecar, within)
            except Exception as error:
                errors.append(error)

        with patch.object(index, "load_index", load):
            first = threading.Thread(target=get, args=(None,))
            second = threading.Thread(target=get, args=(self.out,))
            first.start()
            self.assertTrue(entered.wait(1))
            second.start()
            time.sleep(0.1)
            release.set()
            first.join(3)
            second.join(3)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(overlap, [1, 1])

    def test_cache_retry_releases_failed_load_buffers(self):
        sidecar = self.write([[1.0, 0.0, 0.0]])
        cache = index.IndexCache()
        original = index.load_index
        buffers = []

        def load(*args):
            if not buffers:
                buffer = array.array("f", [1.0])
                buffers.append(weakref.ref(buffer))
                raise OSError("read raced with replacement")
            self.assertIsNone(buffers[0](), "the first exception must release its array before the retry")
            return original(*args)

        with patch.object(index, "load_index", load), patch.object(index, "_pause"):
            cache.get(sidecar)

    def test_a_growing_vectors_file_is_read_with_a_bound_and_refused(self):
        sidecar = self.write([[1.0, 0.0, 0.0]])
        original_open = os.fdopen
        reads = []

        def open_file(fd, *args, **kwargs):
            handle = original_open(fd, *args, **kwargs)
            if os.fstat(fd).st_size != 12:
                return handle
            wrapped = MagicMock(wraps=handle)
            wrapped.__enter__.return_value = wrapped
            wrapped.__exit__.side_effect = lambda *_: handle.close()

            def read(size=-1):
                reads.append(size)
                return handle.read(size)

            def readinto(buffer):
                reads.append(buffer.nbytes)
                with (self.out / index.VECTORS).open("ab") as growing:
                    growing.write(b"x" * 1024)
                return handle.readinto(buffer)

            wrapped.read.side_effect = read
            wrapped.readinto.side_effect = readinto
            return wrapped

        with patch.object(os, "fdopen", open_file), self.assertRaisesRegex(index.InvalidIndex, "changed"):
            index.load_index(sidecar)
        self.assertEqual(reads, [12, 1], "fill the single array, then read one growth sentinel")

    def test_an_index_at_both_limits_loads(self):
        dimensions = 4096
        count = index.MAX_VECTOR_BYTES // (dimensions * 4)
        vector_bytes = count * dimensions * 4
        sidecar = self.write([[1.0, 0.0, 0.0]])
        vector_path = self.out / index.VECTORS
        # Holes represent zero components without allocating a large fixture on disk or in memory.
        with vector_path.open("wb") as handle:
            for row in range(count):
                handle.seek(row * dimensions * 4)
                handle.write(b"\x00\x00\x80\x3f")
            handle.truncate(vector_bytes)
        digest = hashlib.sha256()
        with vector_path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        meta.update(dimensions=dimensions, count=count, chunks=meta["chunks"] * count)
        meta["vectors"]["sha256"] = digest.hexdigest()
        sidecar.write_text(json.dumps(meta), encoding="utf-8")
        loaded = index.load_index(sidecar)
        self.assertEqual((loaded.dimensions, loaded.count, len(loaded.vectors) * 4),
                         (dimensions, count, vector_bytes))

    def test_writer_refuses_loader_limits_before_opening_files(self):
        chunk = {"source": 0, "start": 0, "end": 1, "text": "t"}
        for dimensions, count, message in ((4097, 1, "dimensions"),
                                           (4096, index.MAX_VECTOR_BYTES // (4096 * 4) + 1, "vector bytes"),
                                           (3, index.MAX_INDEX_CHUNKS + 1, "chunks")):
            with self.subTest(dimensions=dimensions, count=count), patch.object(Path, "open") as opened:
                with self.assertRaisesRegex(index.BuildError, message):
                    index.write_index(self.out, model="m", backend="lexical", dimensions=dimensions,
                                      chunking={}, sources=["a.txt"], chunks=[chunk] * count, rows=[])
                opened.assert_not_called()

    def test_writer_stops_before_consuming_an_extra_row(self):
        with self.assertRaisesRegex(index.BuildError, "more vectors"):
            self.write([[1.0, 0.0, 0.0], None],
                       chunks=[{"source": 0, "start": 0, "end": 1, "text": "t"}])
        self.assertFalse(list(self.out.iterdir()), "a refused build leaves no files behind")

    def test_writer_accepts_the_byte_limit_and_preserves_it_on_refusal(self):
        with patch.object(index, "MAX_VECTOR_BYTES", 24):
            sidecar = self.write([[1.0, 0.0, 0.0]] * 2)
            before = sidecar.read_bytes()
            self.assertEqual(index.load_index(sidecar).count, 2)
            with self.assertRaisesRegex(index.BuildError, "vector bytes"):
                self.write([[1.0, 0.0, 0.0]] * 3)
            self.assertEqual(sidecar.read_bytes(), before)
            self.assertEqual(index.load_index(sidecar).count, 2)

    def test_a_vectors_file_that_escapes_the_allowed_directory_is_refused(self):
        sidecar = self.write([[1.0, 0.0, 0.0]])
        outside = Path(self.temp.name) / "outside.f32"
        outside.write_bytes((self.out / index.VECTORS).read_bytes())
        (self.out / index.VECTORS).unlink()
        (self.out / index.VECTORS).symlink_to(outside)
        index.load_index(sidecar)
        with self.assertRaises(index.InvalidIndex):
            index.load_index(sidecar, within=self.out)

    def test_search_orders_by_dot_product_with_stable_ties(self):
        loaded = index.load_index(self.write([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [1.0, 0.0, 0.0]]))
        self.assertEqual(index.search(loaded, [1.0, 0.0, 0.0], 3), [(0, 1.0), (2, 1.0), (1, 0.0)])
        self.assertEqual(index.search(loaded, [0.0, 1.0, 0.0], 1), [(1, 1.0)])

    def test_the_cache_reloads_on_change_and_evicts_the_oldest(self):
        sidecar = self.write([[1.0, 0.0, 0.0]])
        cache = index.IndexCache(limit_bytes=sidecar.stat().st_size + (self.out / index.VECTORS).stat().st_size)
        with patch.object(index, "load_index", wraps=index.load_index) as load:
            first = cache.get(sidecar)
            self.assertIs(cache.get(sidecar), first)
            self.assertEqual(load.call_count, 1)
            stamp = sidecar.stat()
            os.utime(sidecar, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 2_000_000_000))
            self.assertIsNot(cache.get(sidecar), first)
            self.assertEqual(load.call_count, 2)
            other = Path(self.temp.name) / "other"
            index.write_index(other, model="m", backend="lexical", dimensions=3, chunking={"chars": 1000, "overlap": 0},
                              sources=["b.txt"], chunks=[{"source": 0, "start": 0, "end": 1, "text": "u"}],
                              rows=[[0.0, 1.0, 0.0]])
            cache.get(other / index.SIDECAR)
            cache.get(sidecar)
            self.assertEqual(load.call_count, 4, "the budget fits only one index")

    def test_the_cache_does_not_keep_an_over_budget_index(self):
        sidecar = self.write([[1.0, 0.0, 0.0]])
        cache = index.IndexCache(limit_bytes=1)
        with patch.object(index, "load_index", wraps=index.load_index) as load:
            first = cache.get(sidecar)
            self.assertEqual(list(first.vectors), [1.0, 0.0, 0.0])
            self.assertIsNot(cache.get(sidecar), first)
            self.assertEqual(load.call_count, 2, "an oversized index can be used but must not be retained")

    def test_the_cache_notices_a_rebuild_with_the_same_size_and_time(self):
        sidecar = self.write([[1.0, 0.0, 0.0]])
        cache = index.IndexCache()
        self.assertEqual(list(cache.get(sidecar).vectors), [1.0, 0.0, 0.0])
        times = {name: (self.out / name).stat() for name in (index.SIDECAR, index.VECTORS)}
        self.write([[0.0, 1.0, 0.0]])
        for name, stamp in times.items():
            self.assertEqual((self.out / name).stat().st_size, stamp.st_size)
            os.utime(self.out / name, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        # Every rebuild replaces both files by rename, so their inodes change even when size and time do not.
        self.assertEqual(list(cache.get(sidecar).vectors), [0.0, 1.0, 0.0])


class BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = BridgeServer(("127.0.0.1", 0), TOKEN, Settings(capacity=64))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.bridge = Bridge(cls.url, TOKEN)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name) / "data"
        (self.data / "notes").mkdir(parents=True)
        # About 70 chunks of 200 characters across two files, with a word unique to every chunk.
        (self.data / "a.txt").write_text(" ".join(f"alpha{i}x" for i in range(1200)), encoding="utf-8")
        (self.data / "notes" / "b.md").write_text(" ".join(f"beta{i}y" for i in range(900)), encoding="utf-8")
        (self.data / "notes" / "skip.json").write_text('["not indexed"]', encoding="utf-8")
        self.out = self.data / "artifacts" / "idx"

    def tearDown(self):
        self.temp.cleanup()

    def test_builds_every_chunk_in_batches_of_32_against_the_worker(self):
        with patch.object(self.bridge, "run", wraps=self.bridge.run) as run:
            meta = index.build(self.bridge, [self.data], self.out, chars=200, overlap=40)
        loaded = index.load_index(self.out / index.SIDECAR)
        self.assertEqual((loaded.model, loaded.backend, loaded.dimensions), ("hashing-bow-not-a-model", "lexical", 384))
        self.assertEqual(meta["count"], loaded.count)
        self.assertGreater(loaded.count, 64)
        self.assertEqual(run.call_count, -(-loaded.count // 32))
        self.assertEqual(sorted(loaded.sources), ["../../a.txt", "../../notes/b.md"])
        for number in (0, 33, loaded.count - 1):
            chunk = loaded.chunks[number]
            source = (self.out / loaded.sources[chunk["source"]]).resolve()
            self.assertEqual(source.read_text(encoding="utf-8")[chunk["start"]:chunk["end"]], chunk["text"])
            row = loaded.vectors[number * 384:(number + 1) * 384]
            for got, want in zip(row, worker_vector(chunk["text"])):
                self.assertAlmostEqual(got, want, places=6)

    def test_offsets_count_code_points_of_the_file_as_stored(self):
        (self.data / "a.txt").write_bytes("café au lait\r\nsecond line".encode("utf-8"))
        (self.data / "notes" / "b.md").unlink()
        index.build(self.bridge, [self.data / "a.txt"], self.out, chars=200, overlap=0)
        chunk = index.load_index(self.out / index.SIDECAR).chunks[0]
        self.assertEqual((chunk["start"], chunk["end"], chunk["text"]), (0, 25, "café au lait\r\nsecond line"))

    def test_backs_off_when_the_worker_is_at_capacity(self):
        real = self.bridge

        class Flaky:
            calls = 0

            def health(self):
                return real.health()

            def run(self, *args, **kwargs):
                Flaky.calls += 1
                if Flaky.calls == 1:
                    raise BridgeError("full", "capacity_exceeded", 429)
                return real.run(*args, **kwargs)

        with patch.object(index, "_pause") as pause:
            index.build(Flaky(), [self.data / "a.txt"], self.out, chars=200, overlap=0)
        pause.assert_called_once()
        self.assertTrue((self.out / index.SIDECAR).is_file())

    def test_refuses_a_worker_that_changes_model_mid_build_and_writes_nothing(self):
        class Changing:
            calls = 0

            def health(self):
                return {"backend": "lexical", "protocol": 1}

            def run(self, task, payload, *args, **kwargs):
                Changing.calls += 1
                vector = [1.0] + [0.0] * 383
                return {"model": "first" if Changing.calls == 1 else "second", "dimensions": 384,
                        "vectors": [vector for _ in payload["texts"]]}

        with self.assertRaises(index.BuildError):
            index.build(Changing(), [self.data], self.out, chars=200, overlap=0)
        self.assertFalse(self.out.exists() and any(self.out.iterdir()), "a refused build leaves nothing behind")

    def cli(self, *arguments, token=TOKEN):
        environment = {**os.environ, "BRIDGE_URL": self.url, "BRIDGE_TOKEN": token}
        return subprocess.run([sys.executable, "-m", "bridge_mcp.index", "build", *arguments],
                              capture_output=True, text=True, timeout=120, env=environment)

    def test_the_command_line_builds_offline_and_never_prints_the_token(self):
        completed = self.cli(str(self.data / "a.txt"), "--out", str(self.out), "--chunk-chars", "500")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue((self.out / index.SIDECAR).is_file() and (self.out / index.VECTORS).is_file())
        self.assertNotIn(TOKEN, completed.stdout + completed.stderr)
        self.assertEqual(json.loads((self.out / index.SIDECAR).read_text(encoding="utf-8"))["chunking"]["chars"], 500)

    def test_the_command_line_refuses_bad_input_with_exit_code_2(self):
        empty = self.data / "empty"
        empty.mkdir()
        binary = self.data / "binary.txt"
        binary.write_bytes(b"\xff\xfe\x00bad")
        for arguments, token in [((str(empty), "--out", str(self.out)), TOKEN),
                                 ((str(binary), "--out", str(self.out)), TOKEN),
                                 ((str(self.data / "missing.txt"), "--out", str(self.out)), TOKEN),
                                 ((str(self.data / "a.txt"), "--out", str(self.data / "a.txt")), TOKEN),
                                 ((str(self.data / "a.txt"), "--out", str(self.out), "--timeout-s", "0"), TOKEN),
                                 ((str(self.data / "a.txt"), "--out", str(self.out)), "")]:
            with self.subTest(arguments=arguments[0][-12:], token=bool(token)):
                completed = self.cli(*arguments, token=token)
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertIn("bridge_mcp.index:", completed.stderr)
                if "--timeout-s" in arguments:
                    self.assertIn("--timeout-s", completed.stderr)
                self.assertNotIn(TOKEN, completed.stdout + completed.stderr)

    def test_an_unwritable_output_is_a_refusal_not_a_traceback(self):
        locked = self.data / "locked"
        locked.mkdir()
        locked.chmod(0o555)
        try:
            completed = self.cli(str(self.data / "a.txt"), "--out", str(locked / "idx"))
        finally:
            locked.chmod(0o755)
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("bridge_mcp.index: cannot write", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_the_builder_does_not_import_the_mcp_sdk(self):
        completed = subprocess.run([sys.executable, "-c", "import sys, bridge_mcp.index; print(sorted(m for m in sys.modules "
                                    "if m == 'mcp' or m.startswith(('mcp.', 'anyio', 'pydantic'))))"],
                                   capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.stdout.strip(), "[]", completed.stderr)
