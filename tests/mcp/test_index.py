"""The index builder and loader: chunking, the on-disk format, building over the protocol, search and caching."""

import array
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

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
        vectors = lambda sidecar: sidecar.parent / index.VECTORS
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
        cache = index.IndexCache(limit=1)
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
            self.assertEqual(load.call_count, 4, "a limit of one evicts the first index")


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
                                 ((str(self.data / "a.txt"), "--out", str(self.out)), "")]:
            with self.subTest(arguments=arguments[0][-12:], token=bool(token)):
                completed = self.cli(*arguments, token=token)
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertIn("bridge_mcp.index:", completed.stderr)
                self.assertNotIn(TOKEN, completed.stdout + completed.stderr)

    def test_the_builder_does_not_import_the_mcp_sdk(self):
        completed = subprocess.run([sys.executable, "-c", "import sys, bridge_mcp.index; print(sorted(m for m in sys.modules "
                                    "if m == 'mcp' or m.startswith(('mcp.', 'anyio', 'pydantic'))))"],
                                   capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.stdout.strip(), "[]", completed.stderr)
