#!/usr/bin/env bash
# Triggers the resume-curator agent headlessly.
#
# Safe to run more often than your cadence: the agent reads cadence_days from
# curator.json and declines to rebuild if the current base resume is still
# fresh. Firing monthly against a 90-day cadence is the intended setup -- it
# means a laptop asleep on the due date doesn't skip a quarter.
set -euo pipefail

CORPUS="${RESUME_MCP_DATA_DIR:-$HOME/resume-corpus}"
LOG="$CORPUS/logs/curator.log"
mkdir -p "$(dirname "$LOG")"

{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') triggering resume-curator ==="
  cd "$CORPUS"
  RESUME_MCP_DATA_DIR="$CORPUS" claude -p \
    "Use the resume-curator agent to refresh my base resume. Respect the cadence gate in curator.json -- if the current base resume is still within cadence, report that and stop." \
    --allowedTools Read Write Edit Glob Grep \
      mcp__resume__list_available_resumes \
      mcp__resume__retrieve_resume \
      mcp__resume__find_resume \
    --permission-mode acceptEdits
  echo
} >> "$LOG" 2>&1
