from pathlib import Path

import yaml


def _load_yaml(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def test_node_exporter_scrape_uses_compose_service_name() -> None:
    prometheus_config = _load_yaml("config/monitoring/prometheus.yml")
    jobs = {job["job_name"]: job for job in prometheus_config["scrape_configs"]}
    node_targets = jobs["node"]["static_configs"][0]["targets"]

    assert "node-exporter:9100" in node_targets
    assert "localhost:9100" not in node_targets


def test_node_exporter_compose_service_matches_scrape_target() -> None:
    prometheus_config = _load_yaml("config/monitoring/prometheus.yml")
    compose_config = _load_yaml("docker-compose.yml")
    services = compose_config["services"]
    jobs = {job["job_name"]: job for job in prometheus_config["scrape_configs"]}
    node_targets = jobs["node"]["static_configs"][0]["targets"]

    assert "node-exporter" in services
    assert "node-exporter:9100" in node_targets
    node_exporter = services["node-exporter"]
    assert node_exporter["pid"] == "host"
    assert node_exporter.get("network_mode") != "host"
