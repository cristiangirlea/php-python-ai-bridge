"""The measurement driver refuses a row that ends beside a foreign container."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


class RerankDriverTests(unittest.TestCase):
    def test_foreign_container_after_a_row_fails_but_standing_services_are_allowed(self):
        for foreign, expected in (("another-job", 1), ("standing-service", 0)):
            with self.subTest(foreign=foreign), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "scripts").mkdir()
                script = root / "scripts/rerank_measure.sh"
                script.write_text((REPO / "scripts/rerank_measure.sh").read_text())
                trace = root / "trace"
                result = subprocess.run(["bash", "-c", '''
git() { echo test-tree; }
sleep() { return 1; }
docker() {
    printf '%s\\n' "$*" >> "$TRACE"
    case "$*" in
        ps*)
            echo standing-service
            if [ -f "$ROW_DONE" ]; then echo "$FOREIGN"; fi ;;
        *"model-quality python"*) touch "$ROW_DONE"; echo measured-row ;;
        *" ps -q "*) echo worker-id ;;
        exec*) echo 0 ;;
    esac
}
export -f git docker sleep
bash "$SCRIPT"
'''], env={**os.environ, "SCRIPT": str(script), "TRACE": str(trace),
           "ROW_DONE": str(root / "row-done"), "FOREIGN": foreign,
           "BENCH_STANDING": "^standing-service$", "BRIDGE_TOKEN": "test-only-disposable-token-for-driver"},
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, expected, result.stderr)
                commands = trace.read_text().splitlines()
                rows = [line for line in commands if "model-quality python" in line]
                self.assertEqual(len(rows), 1 if expected else 6)
                self.assertTrue(commands[-1].endswith(" --profile model down"))
                if expected:
                    self.assertIn("Foreign containers after measurement", result.stderr)
