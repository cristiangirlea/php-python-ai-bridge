"""A failed diagnostic must not skip the crash drill's cleanup."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


class HaDriverTests(unittest.TestCase):
    def test_cleanup_survives_a_failed_log_command_and_keeps_the_original_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "commands"
            environment = {**os.environ, "HA_TRACE": str(trace), "HA_SCRIPT": str(REPO / "scripts/ha_check.sh")}
            result = subprocess.run(["sh", "-c", '''
docker() {
    printf '%s\\n' "$*" >> "$HA_TRACE"
    case " $* " in
        *" up "*) return 7 ;;
        *" logs "*) return 8 ;;
    esac
    return 0
}
. "$HA_SCRIPT"
'''], env=environment, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 7, result.stderr)
            commands = trace.read_text().splitlines()
            self.assertTrue(any(" logs --tail 100" in command for command in commands))
            self.assertTrue(commands[-1].endswith(" --profile ha down -v"), commands)
