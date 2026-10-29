"""A failed diagnostic must not skip the crash drill's cleanup."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


class HaDriverTests(unittest.TestCase):
    def test_ha_and_tls_oversized_length_matchers_are_identical(self):
        matchers = []
        for example in ("ha", "tls"):
            lines = (REPO / "examples" / example / "Caddyfile").read_text().splitlines()
            matches = [line.split("Content-Length ", 1)[1] for line in lines
                       if line.strip().startswith("@oversized header_regexp Content-Length ")]
            self.assertEqual(len(matches), 1, example)
            matchers.append(matches[0])
        self.assertEqual(*matchers)

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
