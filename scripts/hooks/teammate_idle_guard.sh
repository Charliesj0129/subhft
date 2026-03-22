#!/usr/bin/env bash
# teammate_idle_guard.sh — TeammateIdle hook
# Exit code 2 = send feedback and keep teammate working
# Exit code 0 = allow idle (no action)

set -euo pipefail

python3 -c "
import json, sys

try:
    data = json.load(sys.stdin)
except Exception:
    data = {}

name = data.get('teammate_name', 'unknown')
remaining = int(data.get('tasks_remaining', 0))

if remaining > 0:
    print(json.dumps({
        'systemMessage': f\"Teammate '{name}' has {remaining} task(s) remaining. Resuming work.\",
        'hookSpecificOutput': {
            'hookEventName': 'TeammateIdle',
            'additionalContext': (
                f'You still have {remaining} uncompleted task(s). '
                'Review the task list and pick the next pending task. '
                'Do NOT idle until all assigned tasks are complete. '
                'Focus on accuracy over speed.'
            )
        }
    }))
    sys.exit(2)

print(json.dumps({
    'systemMessage': f\"Teammate '{name}' completed all tasks. Allowing idle.\"
}))
sys.exit(0)
"
