"""The workflows run on the lab's self-hosted runner, as written here never for a fork's pull request, and every job
that drives Docker Compose gets the same pinned, hash-checked plugin wherever the runner lacks one.

The repository is public, so a fork's pull request would run its own code on the runner it is routed to: these
workflows send it to GitHub's disposable runners. A fork can edit them in its pull request, though, so the boundary
that holds is the repository's fork approval policy, not this routing (docs/security.md). Read as text, since the
standard library has no YAML parser; the workflows keep their jobs two spaces in under `jobs:`.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))
ACTION = REPO / ".github" / "actions" / "compose" / "action.yml"
RUNS_ON = ("runs-on: ${{ (github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == "
           "github.repository) && vars.CI_RUNNER || 'ubuntu-latest' }}")


def jobs(text: str) -> dict:
    """Each job's name and its lines."""
    found, name = {}, None
    for line in text.split("\njobs:\n", 1)[1].splitlines():
        header = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if header:
            name = header.group(1)
            found[name] = []
        elif name and line.startswith("    "):
            found[name].append(line.strip())
        elif line.strip() and not line.startswith(" "):
            name = None
    return found


class WorkflowTests(unittest.TestCase):
    def test_the_workflows_are_found(self):
        self.assertEqual([path.name for path in WORKFLOWS], ["model-smoke.yml", "tests.yml"])
        for path in WORKFLOWS:
            self.assertTrue(jobs(path.read_text(encoding="utf-8")), path.name)

    def test_every_job_runs_on_the_lab_runner_unless_a_fork_asks(self):
        for path in WORKFLOWS:
            for name, lines in jobs(path.read_text(encoding="utf-8")).items():
                with self.subTest(workflow=path.name, job=name):
                    self.assertEqual([line for line in lines if line.startswith("runs-on:")], [RUNS_ON])

    def test_every_job_that_runs_compose_ensures_the_plugin_after_checkout_and_before_compose(self):
        for path in WORKFLOWS:
            for name, lines in jobs(path.read_text(encoding="utf-8")).items():
                commands = [i for i, line in enumerate(lines) if "docker compose" in line and not line.startswith("#")]
                if not commands:
                    continue
                with self.subTest(workflow=path.name, job=name):
                    ensure = lines.index("- uses: ./.github/actions/compose")
                    checkout = next(i for i, line in enumerate(lines) if line.startswith("- uses: actions/checkout@"))
                    self.assertLess(checkout, ensure)
                    self.assertLess(ensure, commands[0])

    def test_the_compose_plugin_is_a_pinned_release_checked_by_hash_and_installed_only_where_missing(self):
        text = ACTION.read_text(encoding="utf-8")
        self.assertRegex(text, r"COMPOSE_VERSION: v\d+\.\d+\.\d+\n")
        self.assertRegex(text, r"COMPOSE_SHA256: [0-9a-f]{64}\n")
        self.assertIn('| sha256sum -c -', text)
        self.assertIn("if ! docker compose version", text)
        self.assertIn("using: composite", text)

    def test_tests_run_for_pull_requests_and_for_pushes_to_main_only(self):
        # A push to a pull request's branch would otherwise run every job twice on a one-runner lab.
        text = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertIn("on:\n  push:\n    branches: [main]\n  pull_request:\n", text)

    def test_the_model_workflow_runs_when_the_compose_action_changes(self):
        text = (REPO / ".github" / "workflows" / "model-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("      - .github/actions/compose/**\n", text)

    def test_a_runner_on_another_architecture_is_told_why_it_fails(self):
        text = ACTION.read_text(encoding="utf-8")
        self.assertIn("::error::", text)
        self.assertNotIn('\n          test "$(uname -m)" = x86_64\n', text)

    def test_measurements_name_the_runner_they_ran_on(self):
        text = (REPO / ".github" / "workflows" / "model-smoke.yml").read_text(encoding="utf-8")
        self.assertFalse("BENCH_RUNNER: GitHub-hosted ubuntu-latest" in text, "a measurement names a runner it may not use")
        self.assertEqual(text.count("BENCH_RUNNER: ${{ runner.environment == 'self-hosted' && "
                                    "format('self-hosted {0}', vars.CI_RUNNER) || 'GitHub-hosted ubuntu-latest' }}"), 2)


if __name__ == "__main__":
    unittest.main()
