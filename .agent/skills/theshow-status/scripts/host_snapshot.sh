# Runs ON the host: ssh theshow 'bash -s' < host_snapshot.sh   (read-only)
H="${HOURS:-24}"
red() { sed -E 's/\b[A-Z][12][0-9]{8}\b/***ID***/g; s/\b[0-9]{7,}\b/***N***/g'; }
cd ~/subhft 2>/dev/null || { echo "no ~/subhft"; exit 1; }
echo "== now (UTC) $(date -u +%FT%TZ)"
echo "== containers"
docker ps -a --format '{{.Names}}\t{{.Status}}' | sort
echo "== hft-engine"
docker inspect hft-engine --format 'started={{.State.StartedAt}} restarts={{.RestartCount}} status={{.State.Status}}' 2>&1
echo "== disk"
df -h / "$HOME" 2>/dev/null | awk '!seen[$0]++'
echo "== firing alerts"
curl -s -G localhost:9091/api/v1/query --data-urlencode 'query=ALERTS{alertstate="firing"}' \
 | python3 -c 'import json,sys
try:
  r=json.load(sys.stdin)["data"]["result"]
  print("\n".join(sorted({x["metric"].get("alertname","?")+" "+x["metric"].get("severity","") for x in r})) or "none")
except Exception as e: print("prometheus unreadable:",e)'
echo "== alerts that fired in last ${H}h (alertname samples)"
curl -s -G localhost:9091/api/v1/query --data-urlencode "query=count by (alertname) (count_over_time(ALERTS{alertstate=\"firing\"}[${H}h]))" \
 | python3 -c 'import json,sys
try:
  r=json.load(sys.stdin)["data"]["result"]
  for x in r: print(x["metric"].get("alertname"), x["value"][1])
  if not r: print("none")
except Exception as e: print("unreadable:",e)'
echo "== hft-engine log classes last ${H}h (count level event)"
docker logs --since "${H}h" hft-engine 2>&1 \
 | grep -iE '"level": ?"(error|warning|critical)"|\b(ERROR|WARNING|CRITICAL)\b' \
 | python3 -c 'import sys,re,collections
c=collections.Counter()
for l in sys.stdin:
  lv=re.search(r"(?i)error|warning|critical",l).group(0).lower()
  m=re.search(r"\"event\": ?\"([^\"]{1,80})",l)
  ev=m.group(1) if m else re.sub(r"[0-9:\-T.,Z]+","",l)[:60]
  c[(lv,ev.strip())]+=1
for (lv,e),n in c.most_common(15): print(f"{n:6d} {lv:8s} {e}")' | red
echo "== risk state hints (count)"
docker logs --since "${H}h" hft-engine 2>&1 | grep -oE 'HALT|REDUCE_ONLY|StormGuard[A-Za-z]*|kill_switch[a-z_]*' | sort | uniq -c | head
echo "== deploy ledger tail"
tail -n 3 ~/deploy_ledger.tsv 2>/dev/null | cut -c1-200 | red
