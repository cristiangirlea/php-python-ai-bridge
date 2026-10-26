"""The audit job's script, .github/scripts/audit.sh, run through sh with stub scanners in place of pip-audit and
composer, from a scratch directory holding only the ignore file, so its parser and failure handling are tested
without the network.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / ".github" / "scripts" / "audit.sh"
# Each stub records its arguments and fails when they contain FAIL_ON.
PIP_STUB = 'echo "pip $*" >> "$AUDIT_LOG"\ncase "$*" in *"$FAIL_ON"*) exit 1 ;; esac\n'
COMPOSER_STUB = 'echo "composer $*" >> "$AUDIT_LOG"\n[ "$FAIL_ON" != composer ]\n'


class AuditScriptTests(unittest.TestCase):
    def run_audit(self, ignore: str, fail_on: str = "nothing-fails") -> tuple:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / ".github").mkdir()
            (work / ".github" / "audit-ignore.txt").write_text(ignore, encoding="utf-8")
            (work / "pip.sh").write_text(PIP_STUB, encoding="utf-8")
            (work / "composer.sh").write_text(COMPOSER_STUB, encoding="utf-8")
            sh = shutil.which("sh") or "sh"
            env = dict(os.environ, AUDIT_LOG=str(work / "log"), FAIL_ON=fail_on,
                       PIP_AUDIT_CMD=f"{sh} {work / 'pip.sh'}", COMPOSER_AUDIT_CMD=f"{sh} {work / 'composer.sh'}")
            result = subprocess.run([sh, str(SCRIPT)], cwd=work, env=env, capture_output=True, text=True, timeout=60)
            log = (work / "log").read_text(encoding="utf-8").splitlines() if (work / "log").exists() else []
        return result, log

    def test_all_clean_exits_0_and_audits_every_lock_with_the_accepted_ids(self):
        ignore = "# a comment\n\n   \nPYSEC-2026-1 not reachable: the worker never parses URLs\n  # indented\nGHSA-abcd-1234-efgh dev only"
        result, log = self.run_audit(ignore)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        ignored = "--ignore-vuln PYSEC-2026-1 --ignore-vuln GHSA-abcd-1234-efgh"
        self.assertEqual(log, [
            f"pip --disable-pip -r requirements-model.lock --require-hashes {ignored}",
            f"pip --disable-pip -r requirements-mcp.lock --require-hashes {ignored}",
            'composer ["PYSEC-2026-1","GHSA-abcd-1234-efgh"]',
        ])

    def test_an_empty_ignore_file_passes_no_ids(self):
        result, log = self.run_audit("# only a comment\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(log, [
            "pip --disable-pip -r requirements-model.lock --require-hashes",
            "pip --disable-pip -r requirements-mcp.lock --require-hashes",
            "composer []",
        ])

    def test_any_failing_scanner_fails_the_script_after_every_scanner_ran(self):
        for fail_on in ("requirements-model.lock", "requirements-mcp.lock", "composer"):
            with self.subTest(fail_on=fail_on):
                result, log = self.run_audit("", fail_on)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(len(log), 3, log)

    def test_a_malformed_id_or_one_without_a_reason_stops_before_any_scanner(self):
        for line, message in (("pysec-2026-1 lower case", "::error::malformed id: pysec-2026-1"),
                              ("PYSEC_2026_1 underscores", "::error::malformed id: PYSEC_2026_1"),
                              ("PYSEC-$(id) shell", "::error::malformed id: PYSEC-$(id)"),
                              ("PYSEC-2026-1", "::error::PYSEC-2026-1 has no reason"),
                              ("PYSEC-2026-1   ", "::error::PYSEC-2026-1 has no reason")):
            with self.subTest(line=line):
                result, log = self.run_audit(f"GHSA-abcd-1234-efgh fine\n{line}\n")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(message, result.stdout)
                self.assertEqual(log, [])

    def test_the_defaults_are_the_pinned_scanners(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("\r", text)
        self.assertRegex(text, r'PIP_AUDIT_CMD=\$\{PIP_AUDIT_CMD:-pipx run --spec pip-audit==\d+\.\d+\.\d+ pip-audit\}')
        self.assertRegex(text, r"composer:2@sha256:[0-9a-f]{64}")
        self.assertIn("composer audit --locked --no-interaction", text)


if __name__ == "__main__":
    unittest.main()
