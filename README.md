# resume-mcp

An [MCP](https://modelcontextprotocol.io) server that turns a folder of resumes
into a searchable Markdown corpus your AI tools can actually reason over.

Drop a `.pdf` or `.docx` into `source-resumes/`. The server converts it to
Markdown, indexes it, and keeps watching — so your corpus is always current
without you running anything. Then ask your assistant things like *"which of my
resumes mention Kubernetes?"* or *"pull the variant I sent to that fintech role and compare its
summary to the base one."*

Built for the specific problem of having **many tailored variants** of one
resume and no good way to search across them.

## Why

Most people tailoring resumes end up with a graveyard of near-duplicate PDFs.
The content is valuable — every variant has a bullet you phrased well once and
can't find now — but it's locked in binaries your tools can't read.

This makes that history a first-class corpus:

- **Grounded, not invented.** The assistant quotes what you actually wrote.
- **Always current.** A file watcher reconciles on startup and on change.
- **Your data stays local.** Nothing leaves your machine.

## Tools

| Tool | What it does |
|---|---|
| `list_available_resumes` | Lists every variant in the index. |
| `retrieve_resume(resume_name)` | Returns one variant's full Markdown. |
| `find_resume(query_string)` | Case-insensitive search across names and content, with matching-line excerpts. |

## Install

Requires Python 3.10+.

```bash
git clone https://github.com/msmekka/resume-mcp.git
cd resume-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Try it without your own data

```bash
resume-mcp --data-dir ./examples/sample-data
```

That loads a fictional two-variant corpus. See [`examples/sample-data`](examples/sample-data).

## Set up your own corpus

Your resumes live in a **data directory**, which is deliberately separate from
this repo so your personal history never ends up in version control.

```bash
mkdir -p ~/resume-corpus/source-resumes
cp ~/Documents/resumes/*.pdf ~/resume-corpus/source-resumes/
resume-mcp --data-dir ~/resume-corpus
```

The server creates and maintains:

```
~/resume-corpus/
├── source-resumes/      # you put .pdf / .docx here
├── markdown-resumes/    # generated Markdown corpus
└── config.json          # name -> markdown filename index
```

### Configuration

| Setting | Env var | Flag | Default |
|---|---|---|---|
| Data directory | `RESUME_MCP_DATA_DIR` | `--data-dir` | working directory |
| Server name | `RESUME_MCP_SERVER_NAME` | — | `Resume Corpus Manager` |
| Disable file watcher | — | `--no-watch` | watcher on |

`--data-dir` wins over `RESUME_MCP_DATA_DIR`, which wins over the working
directory. Use `--no-watch` for one-shot/batch contexts (CI, a cron job) where
a background observer thread isn't wanted.

## Connect it to your AI tools

### Claude Code

```bash
claude mcp add resume -- resume-mcp --data-dir ~/resume-corpus
```

### Claude Desktop

In `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "resume": {
      "command": "/absolute/path/to/.venv/bin/resume-mcp",
      "args": ["--data-dir", "/absolute/path/to/resume-corpus"]
    }
  }
}
```

Use absolute paths — the desktop app does not inherit your shell's `PATH` or
virtualenv.

### Anything else that speaks MCP

The server runs over stdio. Point any MCP client at the `resume-mcp` command
with a `--data-dir` argument.

## Keeping a base resume current

The repo also ships a [`resume-curator`](.claude/agents/resume-curator.md) agent
for Claude Code. It synthesizes a general-purpose **base resume** for a target
title — "Engineering Leader", "Staff Data Scientist" — by curating the strongest
existing bullets from across your variants, and refreshes it on a cadence
(default every 3 months).

It curates; it does not write fiction. Every line has to trace back to something
already in your corpus, and the report tells you which gaps it could not fill.

Configure it with `curator.json` in your data directory:

```json
{
  "target_title": "Engineering Leader",
  "cadence_days": 90,
  "output_name": "base-resume",
  "brag_file": "brag.md"
}
```

Then either ask for it on demand:

```
Use the resume-curator agent to refresh my base resume.
```

or schedule it — see [`examples/scheduling`](examples/scheduling), which covers
the one non-obvious setup step and why the trigger fires more often than the
cadence.

## How it works

```
source-resumes/*.pdf|docx
        │
        ├── startup: reconcile_corpus()  — converts anything missing or stale
        │
        └── running: watchdog observer
                 │
                 └── 2s debounce per path
                          │
                          ▼
                    markitdown.convert()
                          │
                          ▼
            markdown-resumes/*.md  +  config.json
```

Two details worth knowing:

- **Debounce.** Word and Pages write a file in bursts, and Pages saves by
  writing a temp file and renaming it into place. Events are coalesced per path
  with a 2-second timer, so a document is converted once it stops changing
  rather than while it's half-written.
- **Stale-only reconcile.** On startup a source is reconverted only if its
  Markdown is missing or older than the source. Hand-edits you make directly to
  files in `markdown-resumes/` survive a restart.

## Supported formats

`.pdf` and `.docx`, via [markitdown](https://github.com/microsoft/markitdown).

Apple Pages (`.pages`) is **not** supported — it's a zip bundle, not a
document. Export to PDF or DOCX first.

## Privacy

Your resume corpus is personal data: full name, address, phone, employers,
dates. This project is built so that data stays out of git — `.gitignore`
excludes `source-resumes/`, `markdown-resumes/`, `config.json`, and every
`.pdf`/`.docx`/`.pages` in the tree. Keeping your data directory outside the
repo entirely (as the setup above does) is the safest arrangement.

The server reads and writes only your local filesystem and makes no network
calls. What your *AI client* does with the text it retrieves is governed by
that client's own privacy terms.

## License

MIT — see [LICENSE](LICENSE).
