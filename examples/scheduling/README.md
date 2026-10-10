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

## Register the MCP server at user scope

The agent reads the corpus through the `resume` MCP server, and a scheduled run
has no interactive session to fall back on. Register it in Claude Code:

```bash
claude mcp add resume --scope user -- \
  /full/path/to/.venv/bin/resume-mcp --data-dir /full/path/to/resume-corpus
```

**`--scope user` is required here, not merely tidier.** The scheduler runs with
your corpus as the working directory, so a `local`-scope registration — the
default, and tied to the directory you ran `claude mcp add` in — is invisible to
it. Avoid `--scope project` entirely: it writes a `.mcp.json` into the current
directory, which in a clone of this repo is an untracked file carrying the
absolute path to your corpus.

The runner passes these three tools to `--allowedTools`, because a headless run
only gets the tools it is given:

```
mcp__resume__list_available_resumes
mcp__resume__retrieve_resume
mcp__resume__find_resume
```

MCP tools are named `mcp__<server>__<tool>`, so renaming the server from
`resume` to anything else means updating those three names in the runner and in
the agent's `tools:` frontmatter.

`draft_resume` is deliberately **not** in that list. It is for per-posting
drafts from the job-search task; the curator writes a base resume with `Write`.

Verify before relying on a scheduled run:

```bash
claude mcp list                                    # resume - Connected
./refresh-base-resume.sh                           # run the real thing once
```

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

Each run appends to `<corpus>/logs/curator.log`.

## Why this runs locally, not in the cloud

Your corpus is local files. A scheduled cloud agent has nothing to read. If you
later host the corpus somewhere reachable, cloud scheduling becomes an option.
