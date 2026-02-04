#!/bin/bash
# HFT Agent Wrapper - Implements "Lifecycle Hooks" for Gemini CLI
# Usage: ./agent_session.sh [task]

MEMORY_FILE=".agent/memory/current_session.md"
HISTORY_FILE=".agent/memory/lessons_learned.md"

echo "🤖 HFT Agent OS Starting..."

# 1. Pre-Flight Hook: Load Context
CONTEXT_PROMPT=""
if [ -f "$MEMORY_FILE" ]; then
    echo "📂 Loading previous session context..."
    CONTEXT_PROMPT="PREVIOUS SESSION CONTEXT:\n$(cat $MEMORY_FILE)\n\n"
fi

if [ -f "$HISTORY_FILE" ]; then
    echo "🧠 Loading long-term lessons..."
    LESSONS=$(tail -n 20 $HISTORY_FILE) # Load last 20 lines to save tokens
    CONTEXT_PROMPT="${CONTEXT_PROMPT}LESSONS LEARNED:\n${LESSONS}\n\n"
fi

# 2. Main Execution
# We inject the memory as a "System Instruction" or prepend it to the user prompt.
# Since we can't easily change system prompt dynamically in CLI args without config,
# we print it for the user to copy-paste OR we use it if we are wrapping the python API.
# For CLI interactive mode, we rely on the Agent READING the files automatically via rules.

echo "✅ Environment Prepared."
echo "💡 To restore memory, the Agent has been instructed to read .agent/memory/current_session.md automatically via .agent/rules/05-project-structure.md"

# 3. Instruction Injection
# We create a temporary instruction file that the Agent is forced to look at
echo "You are the HFT Agent. AUTOMATIC HOOK: Please read .agent/memory/current_session.md immediately to restore context." > .agent/hooks/start_instruction.md

# Start Gemini
python -m hft_platform run
