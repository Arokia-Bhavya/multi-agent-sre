"""
Inspects the label sets on the metrics we plan to build the Metrics Agent
around, so we use real label names instead of guessing.

Run: python inspect_labels.py
"""

from ai_platform.tools.prometheus_client import PrometheusClient

METRICS_TO_INSPECT = [
    "http_server_request_duration_seconds_count",
    "http_server_request_duration_seconds_bucket",
    "container_cpu_utilization_ratio",
    "container_memory_usage_total_bytes",
    "demo_cart_add_item_latency_seconds_count",
]


def main():
    client = PrometheusClient("http://localhost:9090")

    for metric in METRICS_TO_INSPECT:
        print(f"--- {metric} ---")
        try:
            results = client.query(metric)
            if not results:
                print("  (no series returned)")
                continue
            # Show label keys/values from a couple of sample series
            for r in results[:3]:
                print(f"  {r['metric']}")
            print(f"  ... {len(results)} series total\n")
        except Exception as e:
            print(f"  !! query failed: {e}\n")


if __name__ == "__main__":
    main()