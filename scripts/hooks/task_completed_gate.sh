#!/usr/bin/env bash
# task_completed_gate.sh — TaskCompleted hook
# Exit code 2 = reject completion and send feedback (quality gate failed)
# Exit code 0 = allow completion

set -euo pipefail

python3 -c "
import json, sys, re

try:
    data = json.load(sys.stdin)
except Exception:
    data = {}

task_id = data.get('task_id', data.get('id', 'unknown'))
subject = data.get('task_subject', data.get('subject', 'unknown'))
output = data.get('task_output', data.get('result', '')) or ''

issues = []

# Gate 1: Must have output
if not output.strip():
    issues.append('Task has no output/result. Provide a structured report before completing.')

# Gate 2: Security tasks must classify severity
if output and re.search(r'sec|security|vuln|cve|scan', subject, re.I):
    if not re.search(r'CRITICAL|HIGH|MEDIUM|LOW', output):
        issues.append('Security scan task must classify findings by severity (CRITICAL/HIGH/MEDIUM/LOW).')

# Gate 3: Consistency tasks must reference rules
if output and re.search(r'consist|compliance|law|rule|precision|async', subject, re.I):
    if not re.search(r'Core Law|MB-|AWG-|Precision|Async|Boundary|Cache|Allocator', output):
        issues.append('Consistency task must reference specific rules violated (Core Law #N, MB-NN, AWG-NN).')

# Gate 4: Must include file:line references
if output.strip():
    if not re.search(r'[a-zA-Z_/]+\.(py|rs|yaml|toml):\d+', output):
        issues.append('Findings must include file:line references (e.g., src/hft_platform/risk/engine.py:42).')

if issues:
    feedback = '\\n'.join(f'- {i}' for i in issues)
    print(json.dumps({
        'systemMessage': f\"Task '{subject}' ({task_id}) rejected by quality gate.\",
        'hookSpecificOutput': {
            'hookEventName': 'TaskCompleted',
            'additionalContext': (
                'Your task completion was REJECTED by the quality gate. '
                'Fix these issues before marking complete again:\\n'
                + feedback +
                '\\nProvide a structured report with: severity, file:line, '
                'rule reference, evidence snippet, and recommendation.'
            )
        }
    }))
    sys.exit(2)

print(json.dumps({
    'systemMessage': f\"Task '{subject}' ({task_id}) passed quality gate.\"
}))
sys.exit(0)
"
