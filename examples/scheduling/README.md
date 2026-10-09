# Scheduling the curator

The [`resume-curator`](../../.claude/agents/resume-curator.md) agent refreshes a
general-purpose base resume for a target title, on a cadence you set in
`curator.json` (default 90 days).

## Why the trigger and the cadence are separate

The scheduler's job is only to *offer* the agent a chance to run. The agent
reads `cadence_days` itself and declines if the current base resume is still
fresh. So the trigger should fire **more often than your cadence** — monthly
against a 90-day cadence is a good pairing. If your laptop is asleep on the due
date, the next monthly trigger picks it up instead of skipping the quarter.

Triggering is therefore idempotent. Running the script by hand any time is safe.

## Install the agent where the scheduler can find it

**Do this first.** Claude Code discovers agents from `.claude/agents/` in the
working directory and from `~/.claude/agents/` globally. The scheduler runs with
your *corpus* as the working directory, not this repo — so a job will silently
fail to find the agent unless it is installed globally:

```bash
mkdir -p ~/.claude/agents
ln -sfn "$PWD/.claude/agents/resume-curator.md" ~/.claude/agents/resume-curator.md
```

A symlink rather than a copy, so pulling this repo updates the agent too.

## macOS (launchd)

```bash
cp examples/scheduling/refresh-base-resume.sh ~/bin/
# edit the plist: set the script path and your corpus path
cp examples/scheduling/com.example.resume-curator.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.example.resume-curator.plist
```

Remove it with `launchctl unload` on the same path.

## Linux (cron)

```cron
0 9 1 * *  RESUME_MCP_DATA_DIR=$HOME/resume-corpus $HOME/bin/refresh-base-resume.sh
```

## Run it now, ignoring the cadence

```
Use the resume-curator agent to rebuild my base resume now.
```

The words "now", "force", or "rebuild" override the cadence gate.

## Logs

Each run appends to `<corpus>/reviews/curator.log`.

## Why this runs locally, not in the cloud

Your corpus is local files. A scheduled cloud agent has nothing to read. If you
later host the corpus somewhere reachable, cloud scheduling becomes an option.
