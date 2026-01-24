#!/usr/bin/env python3
import subprocess
import json
import sys
import shutil

def check_docker():
    if not shutil.which("docker"):
        return {"status": "unknown", "error": "docker not found"}
    try:
        # Check if containers are running
        result = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return {"status": "error", "error": result.stderr}
        
        # Parse JSON output (sometimes it's line-delimited JSON, sometimes array)
        output = result.stdout.strip()
        containers = []
        if output:
            for line in output.splitlines():
                try:
                    containers.append(json.loads(line))
                except:
                    pass
        
        running = [c for c in containers if c.get("State") == "running"]
        return {
            "status": "ok" if len(running) > 0 else "down",
            "running_count": len(running),
            "total_count": len(containers)
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}

def check_redis():
    if not shutil.which("redis-cli"):
        return {"status": "unknown", "error": "redis-cli not found"}
    try:
        result = subprocess.run(
            ["redis-cli", "-h", "localhost", "ping"],
            capture_output=True, text=True, timeout=2
        )
        return {"status": "ok" if "PONG" in result.stdout else "down"}
    except:
        return {"status": "down"}

def main():
    health = {
        "docker": check_docker(),
        "redis": check_redis(),
        # Add more checks here (Shioaji, ClickHouse port, etc.)
    }
    
    # Determine overall status
    overall = "ok"
    if health["docker"]["status"] != "ok":
        overall = "critical"
    elif health["redis"]["status"] != "ok":
        overall = "degraded"
        
    print(json.dumps({"overall_status": overall, "details": health}, indent=2))

if __name__ == "__main__":
    main()
