"""Unit tests for the Milestone 6 AnomalyDetector; no cluster needed."""

from unittest import TestCase
from unittest.mock import MagicMock

from ai_platform.tools.prometheus_http_client import PrometheusClient
from ai_platform.tools.anomaly_detector import AnomalyDetector


def _range_result(values):
    """Build a fake query_range result with one series of (timestamp, value) pairs."""
    return [{"metric": {}, "values": [[i, str(v)] for i, v in enumerate(values)]}]


class AnomalyDetectorTests(TestCase):
    def setUp(self):
        self.prom = PrometheusClient("http://prometheus.example")
        self.detector = AnomalyDetector(self.prom, z_score_threshold=3.0)

    def test_flags_a_clear_spike_as_anomalous(self):
        # Stable baseline around 0.1, then a huge spike to 5.0.
        history = [0.1] * 20 + [5.0]
        self.prom.query_range = MagicMock(return_value=_range_result(history))
        result = self.detector.detect_cpu_anomaly("checkout")
        self.assertTrue(result["is_anomaly"])
        self.assertEqual(result["current_value"], 5.0)
        self.assertEqual(result["container_name"], "checkout")
        self.assertEqual(result["metric"], "cpu_utilization")

    def test_stable_history_is_not_anomalous(self):
        history = [0.5, 0.51, 0.49, 0.5, 0.52, 0.48, 0.5]
        self.prom.query_range = MagicMock(return_value=_range_result(history))
        result = self.detector.detect_memory_anomaly("checkout")
        self.assertFalse(result["is_anomaly"])

    def test_insufficient_history_is_not_anomalous(self):
        self.prom.query_range = MagicMock(return_value=_range_result([1.0]))
        result = self.detector.detect_latency_anomaly("checkout")
        self.assertFalse(result["is_anomaly"])
        self.assertEqual(result["reason"], "insufficient history")

    def test_no_data_is_not_anomalous(self):
        self.prom.query_range = MagicMock(return_value=[])
        result = self.detector.detect_request_rate_anomaly("checkout")
        self.assertFalse(result["is_anomaly"])

    def test_nan_values_are_filtered_out(self):
        history = ["0.1", "0.1", "NaN", "0.1", "0.1"]
        self.prom.query_range = MagicMock(return_value=[
            {"metric": {}, "values": [[i, v] for i, v in enumerate(history)]}
        ])
        result = self.detector.detect_cpu_anomaly("checkout")
        self.assertEqual(result["sample_size"], 3)  # 4 valid values, last is "current"

    def test_zero_variance_baseline_does_not_divide_by_zero(self):
        history = [0.3] * 10 + [0.3]
        self.prom.query_range = MagicMock(return_value=_range_result(history))
        result = self.detector.detect_cpu_anomaly("checkout")
        self.assertEqual(result["z_score"], 0.0)
        self.assertFalse(result["is_anomaly"])

    def test_scan_service_combines_all_checks(self):
        stable = [0.5] * 10
        self.prom.query_range = MagicMock(return_value=_range_result(stable))
        result = self.detector.scan_service("checkout")
        self.assertEqual(result["service_name"], "checkout")
        self.assertEqual(set(result["checks"]), {"cpu", "memory", "latency", "request_rate"})
        self.assertFalse(result["has_anomaly"])

    def test_scan_service_has_anomaly_true_if_any_check_flags(self):
        def fake_query_range(query, start, end, step="1m"):
            if "cpu_utilization" in query:
                return _range_result([0.1] * 10 + [9.0])
            return _range_result([0.5] * 10)

        self.prom.query_range = MagicMock(side_effect=fake_query_range)
        result = self.detector.scan_service("checkout")
        self.assertTrue(result["has_anomaly"])
        self.assertTrue(result["checks"]["cpu"]["is_anomaly"])
