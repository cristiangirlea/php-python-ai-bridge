"""The workflows run on GitHub's hosted runners, every job of them, with no self-hosted runner and no runner variable.

The repository is public: GitHub does not meter Actions minutes for it, and a fork's pull request runs its own code
on a disposable runner of GitHub's, never on a machine of the maintainer's. Nothing here installs Docker Compose,
since GitHub's images ship the plugin. Read as text, since the standard library has no YAML parser; the workflows
keep their jobs two spaces in under `jobs:`.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))
RUNS_ON = "runs-on: ubuntu-latest"


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

    def test_every_job_runs_on_github_hosted_ubuntu(self):
        for path in WORKFLOWS:
            for name, lines in jobs(path.read_text(encoding="utf-8")).items():
                with self.subTest(workflow=path.name, job=name):
                    self.assertEqual([line for line in lines if line.startswith("runs-on:")], [RUNS_ON])

    def test_no_workflow_names_a_self_hosted_runner_or_a_runner_variable(self):
        # A `runs-on` expression is how a self-hosted runner or a fallback to one would come back.
        for path in WORKFLOWS:
            text = path.read_text(encoding="utf-8")
            with self.subTest(workflow=path.name):
                self.assertNotIn("vars.CI_RUNNER", text)
                self.assertNotIn("runner.environment", text)
                self.assertNotRegex(text, r"(?i)self-hosted")
                self.assertNotRegex(text, r"runs-on: *\$\{\{")
                self.assertNotRegex(text, r"runs-on: *\[")

    def test_no_workflow_installs_compose_through_a_local_action(self):
        # GitHub's runner images ship the Docker Compose plugin; the lab-only install action is gone with the lab.
        self.assertFalse((REPO / ".github" / "actions").exists())
        for path in WORKFLOWS:
            with self.subTest(workflow=path.name):
                self.assertNotIn(".github/actions/", path.read_text(encoding="utf-8"))

    def test_every_job_that_runs_compose_checks_the_repository_out_first(self):
        for path in WORKFLOWS:
            for name, lines in jobs(path.read_text(encoding="utf-8")).items():
                commands = [i for i, line in enumerate(lines) if "docker compose" in line and not line.startswith("#")]
                if not commands:
                    continue
                with self.subTest(workflow=path.name, job=name):
                    checkout = next(i for i, line in enumerate(lines) if line.startswith("- uses: actions/checkout@"))
                    self.assertLess(checkout, commands[0])

    def test_tests_run_for_pull_requests_and_for_pushes_to_main_only(self):
        # A push to a pull request's branch would otherwise run every job twice.
        text = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertIn("on:\n  push:\n    branches: [main]\n  pull_request:\n", text)

    def test_measurements_name_the_runner_they_ran_on(self):
        text = (REPO / ".github" / "workflows" / "model-smoke.yml").read_text(encoding="utf-8")
        self.assertEqual(text.count("BENCH_RUNNER: GitHub-hosted ubuntu-latest\n"), 2)
        self.assertEqual(text.count("BENCH_RUNNER:"), 2)


if __name__ == "__main__":
    unittest.main()
