---
description: Token usage and cost by work unit. New records are grouped into work units automatically by Claude (sonnet). Pass a work name to list its prompts
argument-hint: [work name]
allowed-tools: Bash(python3:*)
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/tracker.py" report $ARGUMENTS`

Show the result above as is. No interpretation, no extra work.
