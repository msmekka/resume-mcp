# Wiring the server into a Cowork scheduled job

How to let a scheduled Claude Desktop (Cowork) task read your resume corpus and
draft a tailored resume — for example, a daily job-search task that finds a
posting and produces a resume for it.

This is written to be followed by hand, so you understand each piece rather than
inheriting a config you can't debug.

## The one idea that makes this work

A Cowork task runs in a sandboxed VM. Its filesystem view is the Cowork files
directory (`coworkUserFilesPath`, usually `~/Documents/Claude`) — **not** your
whole home directory. So a task cannot open `~/resume-corpus/markdown-resumes/`
by path, no matter how the path is written.

MCP servers are different. Claude Desktop launches them as **host** processes,
outside that sandbox, and exposes only their *tools* to the task. So the server
reads your corpus with your normal file permissions, and the task calls
`find_resume` instead of opening files.

That's the whole trick: **don't try to give the task your files, give it tools.**
Your corpus can live anywhere on disk and never needs to move, sync, or be
copied into the Cowork directory.

```
   Cowork task (in VM)                    host
 ┌──────────────────────┐        ┌───────────────────────────┐
 │ find_resume(...)     │───────>│ resume-mcp process        │
 │ retrieve_resume(...) │<───────│   reads ~/resume-corpus/  │
 │ draft_resume(...)    │        │   writes output_dir/jobs/ │
 └──────────────────────┘        └───────────────────────────┘
      sees only tools                 sees the real filesystem
```

## Step 1 — Get the absolute path to the server

Claude Desktop does not inherit your shell's `PATH` or your activated
virtualenv, so a bare `resume-mcp` will not be found. You need the full path.

```bash
cd /path/to/resume-mcp          # the cloned repo
source .venv/bin/activate
which resume-mcp
```

Copy that path. Confirm it runs on its own before going further:

```bash
/full/path/to/.venv/bin/resume-mcp review --data-dir ~/resume-corpus
```

If that prints a line about generated resumes, the server works. If it fails
here, fix it now — a broken server inside Desktop gives you no useful error.

## Step 2 — Register it in the Desktop config

The file is at:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

It likely already has keys in it (`coworkUserFilesPath`, `preferences`, …).
**Add to it; do not replace it.** Clobbering this file resets your Cowork
settings.

```json
{
  "coworkUserFilesPath": "/Users/you/Documents/Claude",
  "preferences": { "...": "leave whatever is already here" },
  "mcpServers": {
    "resume": {
      "command": "/full/path/to/.venv/bin/resume-mcp",
      "args": ["--data-dir", "/full/path/to/resume-corpus"]
    }
  }
}
```

Both paths must be absolute. `~` is not expanded here.

If you'd rather not hand-edit JSON, this does the merge safely:

```bash
python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home()/"Library/Application Support/Claude/claude_desktop_config.json"
d = json.loads(p.read_text()) if p.exists() else {}
d.setdefault("mcpServers", {})["resume"] = {
    "command": "/full/path/to/.venv/bin/resume-mcp",
    "args": ["--data-dir", "/full/path/to/resume-corpus"],
}
p.write_text(json.dumps(d, indent=2) + "\n")
print(json.dumps(d["mcpServers"], indent=2))
PY
```

Validate the result before restarting — a malformed config is silently ignored:

```bash
python3 -m json.tool ~/Library/Application\ Support/Claude/claude_desktop_config.json >/dev/null \
  && echo "config is valid JSON"
```

## Step 3 — Restart Claude Desktop

Config is read at launch. Quit completely (not just close the window) and
reopen. Until you do this, nothing below will work.

## Step 4 — Confirm the task can see the tools

In a normal Desktop chat, ask:

> List my available resumes.

You should get your corpus back. If you do, the tools are reachable and the
remaining work is all in the task file.

Four tools become available:

| Tool | Use in a job-search task |
|---|---|
| `list_available_resumes` | See what variants exist. |
| `find_resume` | Find the variants that already speak to this posting. |
| `retrieve_resume` | Read those variants in full. |
| `draft_resume` | Save the tailored draft outside the corpus. |

## Step 5 — Authorize the write in your task file

Scheduled Cowork tasks run unattended, so the safe default is that they only
read. A task preamble typically says something like: *write actions (MCP tools
that send, post, create, update, or delete) are only taken if the task file asks
for that specific action.*

`draft_resume` is a write. So your task file has to ask for it by name —
otherwise a correctly-behaving run will skip it. Add an explicit step:

```markdown
6. Draft a tailored resume for the posting using the `resume` MCP server.
   This is an explicitly authorized write action for this task.
```

## Step 6 — Tell it how to build the resume

The sequence that matters is read-the-corpus-first. Add a section like:

```markdown
TAILORED RESUME DRAFT
Build the draft from the corpus, not from the candidate profile in this file.
The profile is for screening postings; the corpus holds the sentences the
candidate actually uses, and those are what belong in a resume.

1. Call `list_available_resumes`.
2. Call `find_resume` with two or three phrases from the posting.
3. Call `retrieve_resume` on the closest variants and read them in full.
4. Assemble the draft: lead with what the posting asks for, order the rest by
   relevance, and drop what is irrelevant rather than rewording it.
5. Call `draft_resume` with the company, role, posting URL, and the Markdown.

Rules:
- Every bullet must trace to text already in the corpus. Do not invent an
  achievement, metric, date, title, employer, or technology.
- Keep metrics exactly as the corpus states them. If two variants disagree, use
  neither and note the conflict.
- Never run `resume-mcp promote`, never write to the corpus or its
  `config.json`, and never copy a draft into `markdown-resumes/`.
- If the `resume` server is unavailable, say so in one line and deliver the rest
  of the report. Do not write a resume from the profile text instead.
```

That last rule matters more than it looks. Without it, an unreachable server
leads to a confident resume invented from the profile summary — which is exactly
the failure this whole design exists to prevent.

## Step 7 — Check the output

After the next run:

```bash
resume-mcp review --data-dir ~/resume-corpus
```

Drafts appear under `output_dir/jobs/`, dated and named after the company and
role. They are **not** in your corpus: `find_resume` won't return them, and the
next run can't read one as source material. When you actually apply with one:

```bash
resume-mcp promote 2026-10-09-company-role --data-dir ~/resume-corpus
```

That prints it, asks for confirmation, and only then indexes it.

## Troubleshooting

**Tools don't appear.** Restart Desktop completely. Then check the config is
valid JSON (Step 2) — an invalid file is ignored without an error.

**"command not found" or the server never starts.** The `command` is not an
absolute path, or points at a venv that was moved. Virtualenvs are not
relocatable: if you renamed or moved the directory, rebuild it.

**Tools appear but return "index not built".** `--data-dir` points somewhere
without a `config.json`. Run the server once from a terminal against that
directory and watch it convert your sources.

**The task reads the corpus but writes no resume.** The task file doesn't
authorize the write by name (Step 5).

**The draft contains things you never wrote.** The task is drafting from a
profile summary in the task file rather than from the corpus. Tighten Step 6 and
keep the "if unavailable, say so" rule.
