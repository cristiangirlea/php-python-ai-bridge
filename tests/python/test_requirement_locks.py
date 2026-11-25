"""Each requirements-*.txt pin is the version its hash lock installs.

The builds install from the .lock files only, so a manifest bumped without its lock (as a Dependabot pip update is)
would name a version nothing installs while CI tests the old one.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9.+!-]+)(?=\s|$)")


def pins(text: str) -> dict[str, str]:
    # Anything but a blank line, a comment or an exact pin is refused, so a range or an extras line cannot slip past.
    found = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = PIN.match(line)
        if not match:
            raise ValueError(f"not an exact pin: {line}")
        found[re.sub(r"[-_.]+", "-", match.group(1)).lower()] = match.group(2)
    return found


def read(path: Path) -> dict[str, str]:
    return pins(path.read_text(encoding="utf-8"))


class LockTests(unittest.TestCase):
    def manifests(self) -> list[Path]:
        found = sorted(REPO.glob("requirements-*.txt"))
        self.assertEqual([path.name for path in found],
                         ["requirements-dev.txt", "requirements-mcp.txt", "requirements-model.txt"])
        return found

    def test_every_manifest_pin_is_the_version_its_lock_installs(self):
        for manifest in self.manifests():
            lock = manifest.with_suffix(".lock")
            locked = read(lock)
            wanted = read(manifest)
            self.assertTrue(wanted, manifest.name)
            for name, version in wanted.items():
                with self.subTest(manifest=manifest.name, package=name):
                    self.assertEqual(locked.get(name), version,
                                     f"{manifest.name} pins {name}=={version}; regenerate {lock.name} to match")

    def test_every_locked_wheel_is_pinned_by_hash(self):
        for manifest in self.manifests():
            lock = manifest.with_suffix(".lock")
            for line in lock.read_text(encoding="utf-8").splitlines():
                if line and not line.startswith("#"):
                    with self.subTest(lock=lock.name, line=line.split(" ")[0]):
                        self.assertRegex(line, r"^\S+==\S+( --hash=sha256:[0-9a-f]{64})+$")

    def test_a_package_two_locks_share_has_one_version(self):
        # py-analysis installs the dev and MCP locks together.
        dev, mcp = read(REPO / "requirements-dev.lock"), read(REPO / "requirements-mcp.lock")
        for name in dev.keys() & mcp.keys():
            with self.subTest(package=name):
                self.assertEqual(dev[name], mcp[name])


class PinTests(unittest.TestCase):
    def test_exact_pins_comments_and_blank_lines_are_read(self):
        text = "# heading\n\nOnnx_Runtime==1.32.0  # inline\nruff==0.17.5 --hash=sha256:" + "0" * 64 + "\n"
        self.assertEqual(pins(text), {"onnx-runtime": "1.32.0", "ruff": "0.17.5"})

    def test_anything_but_an_exact_pin_is_refused(self):
        for line in ("ruff>=0.17", "mcp[cli]==2.3.0", "numpy==2.5.5; python_version >= '3.12'", "-r other.txt"):
            with self.subTest(line=line), self.assertRaisesRegex(ValueError, "not an exact pin"):
                pins(line)


if __name__ == "__main__":
    unittest.main()
