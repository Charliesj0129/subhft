global:
  resolve_timeout: 5m
route:
  group_by: ['alertname']
  group_wait: 10s
  group_interval: 1m
  repeat_interval: 1h
  receiver: 'default-receiver'
  routes:
  - match:
      severity: critical
    receiver: 'critical-receiver'
    repeat_interval: 5m
receivers:
- name: 'critical-receiver'
  webhook_configs:
  - url: '${HFT_ALERT_WEBHOOK_URL}'
    send_resolved: true
- name: 'default-receiver'
  webhook_configs:
  - url: '${HFT_ALERT_WEBHOOK_URL}'
    send_resolved: true
