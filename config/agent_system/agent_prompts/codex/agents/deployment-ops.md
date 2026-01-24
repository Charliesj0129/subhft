---
name: deployment-ops
description: Deploy and validate the stack with docker compose.
tools: Bash, Read
---

# Deployment Ops Agent

Use this agent to deploy and validate runtime services.

## Steps

- make start or docker compose up -d --build
- docker compose ps
- curl http://localhost:9090/metrics
- Check logs for symbols path and Shioaji login
