# ShioajiWatchdogThreadDown
## Symptom
Alert `ShioajiWatchdogThreadDown` fired.
## Investigation
1. Check `docker compose logs hft-engine`
2. Check `docker compose ps` for service health
3. Check Prometheus metrics
## Remediation
Investigate root cause and apply fix.
## Escalation
Escalate if unresolved within 15 minutes.
