"""scripts/public_data.py pins the public datasets the quality measurements read, and refuses anything else.

These run offline: the download is a stub, so they check the pins, the licence gate and the hash checks, never the
network.
"""

import hashlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("public_data", REPO / "scripts" / "public_data.py")
public_data = importlib.util.module_from_spec(spec)
spec.loader.exec_module(public_data)


def pinned(data: bytes, licence: str = "CC-BY-4.0") -> dict:
    return {"file": "sample.bin", "url": "https://example.org/sample.bin", "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "licence": licence, "licence_source": "a test"}


class PinTests(unittest.TestCase):
    def test_every_dataset_is_pinned_over_https_with_a_hash_a_size_and_a_free_licence(self):
        self.assertEqual(set(public_data.DATASETS), {"arguana", "wnut17", "squad"})
        for name, entry in public_data.DATASETS.items():
            with self.subTest(dataset=name):
                self.assertTrue(entry["url"].startswith("https://"))
                self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
                self.assertGreater(entry["bytes"], 0)
                self.assertIn(entry["licence"], public_data.FREE_LICENCES)
                self.assertTrue(entry["licence_source"])
                self.assertEqual(Path(entry["file"]).name, entry["file"])

    def test_a_github_file_is_pinned_to_a_commit_not_a_branch(self):
        self.assertRegex(public_data.DATASETS["wnut17"]["url"],
                         r"^https://raw\.githubusercontent\.com/[^/]+/[^/]+/[0-9a-f]{40}/")

    def test_no_non_commercial_or_no_derivatives_licence_counts_as_free(self):
        self.assertEqual({licence for licence in public_data.FREE_LICENCES if "NC" in licence or "ND" in licence}, set())

    def test_the_data_directory_is_the_cache_unless_set(self):
        self.assertEqual(public_data.data_dir({}), REPO / ".cache" / "datasets")
        self.assertEqual(public_data.data_dir({"QUALITY_DATA_DIR": "/cache/datasets"}), Path("/cache/datasets"))


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.data = b"token\tO\n"

    def fetch(self, entry, served: bytes):
        with mock.patch.dict(public_data.DATASETS, {"sample": entry}), \
                mock.patch.object(public_data, "urlopen", return_value=io.BytesIO(served)) as urlopen:
            return public_data.fetch("sample", self.directory), urlopen

    def test_matching_bytes_are_stored(self):
        path, urlopen = self.fetch(pinned(self.data), self.data)
        self.assertEqual(path, self.directory / "sample.bin")
        self.assertEqual(path.read_bytes(), self.data)
        self.assertEqual(urlopen.call_args.args[0], "https://example.org/sample.bin")
        self.assertEqual(list(self.directory.iterdir()), [path])

    def test_a_stored_file_that_matches_is_not_downloaded_again(self):
        (self.directory / "sample.bin").write_bytes(self.data)
        _, urlopen = self.fetch(pinned(self.data), b"")
        urlopen.assert_not_called()

    def test_different_bytes_are_refused_and_nothing_is_stored(self):
        for served in (b"token\tB-person\n", self.data + b"x", self.data[:-1]):
            with self.subTest(served=served), self.assertRaises(SystemExit):
                self.fetch(pinned(self.data), served)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_a_stored_file_that_changed_is_replaced(self):
        (self.directory / "sample.bin").write_bytes(b"stale")
        path, urlopen = self.fetch(pinned(self.data), self.data)
        urlopen.assert_called_once()
        self.assertEqual(path.read_bytes(), self.data)

    def test_a_dataset_under_a_licence_that_is_not_free_is_never_downloaded(self):
        with self.assertRaisesRegex(SystemExit, "CC-BY-NC-2.0"):
            self.fetch(pinned(self.data, "CC-BY-NC-2.0"), self.data)


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())

    def test_the_measurements_read_only_the_pinned_bytes(self):
        data = b"query-id\tcorpus-id\tscore\n"
        with mock.patch.dict(public_data.DATASETS, {"sample": pinned(data)}):
            with self.assertRaisesRegex(SystemExit, "quality-fetcher"):
                public_data.read("sample", self.directory)
            (self.directory / "sample.bin").write_bytes(data)
            self.assertEqual(public_data.read("sample", self.directory), data)
            (self.directory / "sample.bin").write_bytes(data.upper())
            with self.assertRaises(SystemExit):
                public_data.read("sample", self.directory)


if __name__ == "__main__":
    unittest.main()
