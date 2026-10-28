"""Keep the dashboard's worker metrics aligned with the protocol."""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class DashboardMetricsTest(unittest.TestCase):
    def test_alert_metrics_and_group_labels_are_documented(self):
        protocol = (ROOT / "docs/protocol.md").read_text(encoding="utf-8")
        documented = {}
        for metric, kind, labels in re.findall(r"\| `([a-z_]+)` \| (\w+) \|([^|\n]*)\|", protocol):
            names = set(re.findall(r"`([a-z_]+)(?:=[^`]*)?`", labels)) | {"job", "instance"}
            documented[metric] = names
            if kind == "histogram":
                documented[metric + "_bucket"] = names | {"le"}
        alerts = (ROOT / "deploy/prometheus/alerts.yml").read_text(encoding="utf-8")
        expressions = re.findall(r"^        expr: (.+)$", alerts, re.MULTILINE)
        self.assertEqual(len(expressions), 7)
        for expression in expressions:
            metrics = re.findall(r"\b(?:bridge_[a-z_]+|up)\b", expression)
            self.assertTrue(metrics, expression)
            for metric in metrics:
                self.assertIn(metric, documented)
                for labels in re.findall(r"\bby\s*\(([^)]+)\)", expression):
                    self.assertLessEqual({label.strip() for label in labels.split(",")}, documented[metric])

    def test_alert_thresholds_allow_the_measured_load_and_use_a_histogram_bound(self):
        alerts = (ROOT / "deploy/prometheus/alerts.yml").read_text(encoding="utf-8")
        owner = re.search(r"alert: BridgeOwnerRejected\n        expr: .* > ([0-9.]+)", alerts)
        latency = re.search(r"alert: BridgeJobLatencyHigh\n        expr: .* > ([0-9.]+)", alerts)
        self.assertGreater(float(owner[1]), 159 / 600)
        self.assertEqual(float(latency[1]), 5)
        self.assertGreater(float(latency[1]), 2.90)
        self.assertRegex(alerts, r"alert: BridgeResultsNearCapacity\n"
                                r"        expr: bridge_results_retained >= 0\.9 \* bridge_result_capacity\n"
                                r"        for: 10m\n")

    def test_import_selects_a_prometheus_source_for_every_panel(self):
        dashboard = json.loads((ROOT / "deploy/grafana/bridge.json").read_text(encoding="utf-8"))
        self.assertEqual(dashboard["__inputs"], [{"name": "DS_PROMETHEUS", "label": "Prometheus",
                                                "type": "datasource", "pluginId": "prometheus",
                                                "pluginName": "Prometheus"}])
        for panel in dashboard["panels"]:
            self.assertEqual(panel["datasource"], {"type": "prometheus", "uid": "${DS_PROMETHEUS}"})
            refs = [target["refId"] for target in panel["targets"]]
            self.assertEqual(len(refs), len(set(refs)))

    def test_dashboard_metrics_are_documented(self):
        protocol = (ROOT / "docs/protocol.md").read_text(encoding="utf-8")
        documented = set(re.findall(r"\| `(bridge_[a-z_]+)` \|", protocol))
        dashboard = json.loads((ROOT / "deploy/grafana/bridge.json").read_text(encoding="utf-8"))
        expressions = [target["expr"] for panel in dashboard["panels"] for target in panel["targets"]]
        self.assertTrue(expressions)
        for expression in expressions:
            metrics = set(re.findall(r"\bbridge_[a-z_]+\b", expression))
            self.assertTrue(metrics, expression)
            for metric in metrics:
                self.assertIn(re.sub(r"_(bucket|sum|count)$", "", metric), documented)
        self.assertTrue(any({"bridge_results_retained", "bridge_result_capacity"}
                            <= {target["expr"] for target in panel["targets"]}
                            for panel in dashboard["panels"]))


if __name__ == "__main__":
    unittest.main()
