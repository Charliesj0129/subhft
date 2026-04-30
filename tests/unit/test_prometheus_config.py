from pathlib import Path

import yaml


def test_node_exporter_scrape_uses_compose_service_name() -> None:
    data = yaml.safe_load(
        Path("config/monitoring/prometheus.yml").read_text(encoding="utf-8")
    )
    jobs = {job["job_name"]: job for job in data["scrape_configs"]}
    node_targets = jobs["node"]["static_configs"][0]["targets"]

    assert "node-exporter:9100" in node_targets
    assert "localhost:9100" not in node_targets
