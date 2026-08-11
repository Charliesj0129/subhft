#!/usr/bin/env bash
# task_completed_gate.sh — TaskCompleted hook
# Exit code 2 = reject completion and send feedback (quality gate failed)
# Exit code 0 = allow completion
#
# 2026-08-11: the gate was unsatisfiable, and silently so. Gate 1 required
# 'task_output'/'result', but TaskUpdate — the only way a task is completed —
# carries its report in the description, so every completion was rejected with
# "Task has no output/result" no matter what was written. Three closes in one
# session looked like they succeeded: the rejection JSON goes to stdout, while
# an exit-2 hook surfaces *stderr* to the model, so the caller saw only "No
# stderr output" and a status that never changed. Hence two fixes below: read
# the report from whichever field the host actually sends, and make a rejection
# visible on stderr so this gate can never fail closed in silence again.

set -euo pipefail

python3 -c "
import json, sys, re

try:
    data = json.load(sys.stdin)
except Exception:
    data = {}

task_id = data.get('task_id', data.get('id', 'unknown'))
subject = data.get('task_subject', data.get('subject', 'unknown'))

# The host has used more than one name for the report text and TaskUpdate sends
# it as the description. Take the first non-empty one rather than assuming a
# single key — assuming one key is what made this gate unsatisfiable.
output = ''
for key in ('task_output', 'result', 'output', 'task_description', 'description'):
    value = data.get(key)
    if isinstance(value, str) and value.strip():
        output = value
        break

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
    if not re.search(r'[a-zA-Z_/]+\.(py|rs|yaml|toml|sh|md):\d+', output):
        issues.append('Findings must include file:line references (e.g., src/hft_platform/risk/engine.py:42).')

if issues:
    feedback = '\\n'.join(f'- {i}' for i in issues)
    advice = (
        'Your task completion was REJECTED by the quality gate. '
        'Fix these issues before marking complete again:\\n'
        + feedback +
        '\\nProvide a structured report with: severity, file:line, '
        'rule reference, evidence snippet, and recommendation.'
    )
    # stderr as well as stdout: an exit-2 hook surfaces stderr to the model, so
    # a rejection that only printed JSON on stdout read as a silent success.
    print(f\"Task '{subject}' ({task_id}) rejected by quality gate.\\n{advice}\", file=sys.stderr)
    print(json.dumps({
        'systemMessage': f\"Task '{subject}' ({task_id}) rejected by quality gate.\",
        'hookSpecificOutput': {
            'hookEventName': 'TaskCompleted',
            'additionalContext': advice
        }
    }))
    sys.exit(2)

print(json.dumps({
    'systemMessage': f\"Task '{subject}' ({task_id}) passed quality gate.\"
}))
sys.exit(0)
"
