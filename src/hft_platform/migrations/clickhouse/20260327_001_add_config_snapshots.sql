CREATE TABLE IF NOT EXISTS hft.config_snapshots (
    boot_ts      DateTime64(3),
    config_hash  String,
    git_sha      String,
    env_json     String,
    yaml_json    String
) ENGINE = MergeTree()
ORDER BY boot_ts
TTL boot_ts + INTERVAL 1 YEAR;
