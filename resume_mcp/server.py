import argparse
import collections
import itertools
import json
import logging
import os
import re
import shutil
import sys
from datetime import date
import threading
from pathlib import Path

from fastmcp import FastMCP
from markitdown import MarkItDown
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

SUPPORTED_FILE_TYPES = ['.pdf', '.docx']
DEFAULT_SERVER_NAME = "Resume Corpus Manager"
CURATOR_FILE_NAME = "curator.json"
DEFAULT_OUTPUT_DIR = "base-resumes"

logger = logging.getLogger(__name__)

#Paths are resolved at startup, not import, so --data-dir can override the
#environment and nothing is created just by importing this module.
DATA_DIR = None
SOURCE_DIR = None
CORPUS_DIR = None
CONFIG_FILE = None

markitdown = MarkItDown()
mcp = FastMCP(os.getenv("RESUME_MCP_SERVER_NAME", DEFAULT_SERVER_NAME))


def resolve_data_dir(explicit: str | None = None) -> Path:
    """Picks the corpus location: --data-dir, then RESUME_MCP_DATA_DIR, then cwd.

    The default is the working directory rather than the package directory so
    that an installed copy of this server never writes into site-packages.
    """
    raw = explicit or os.getenv("RESUME_MCP_DATA_DIR") or os.getcwd()
    return Path(raw).expanduser().resolve()


def configure(data_dir: Path, create: bool = True):
    """Binds the module to a data directory."""
    global DATA_DIR, SOURCE_DIR, CORPUS_DIR, CONFIG_FILE
    DATA_DIR = data_dir
    SOURCE_DIR = DATA_DIR / "source-resumes"
    CORPUS_DIR = DATA_DIR / "markdown-resumes"
    CONFIG_FILE = DATA_DIR / "config.json"
    if create:
        SOURCE_DIR.mkdir(parents=True, exist_ok=True)
        CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def load_index() -> dict:
    """Reads the name -> markdown-filename index, tolerating a missing or torn file."""
    if CONFIG_FILE is None or not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        logger.warning("Index at %s is not valid JSON; treating it as empty", CONFIG_FILE)
        return {}



#--------PDF EXTRACTION QUALITY-------------
#Apple Pages, and some other PDF producers, embed fonts whose glyphs carry no
#usable Unicode mapping. Extraction then yields a substitute character where a
#real one stood, and the substitute differs per glyph and per document:
#"Pla,orm", "Na>ve", "integra@on", "Innova=on", "producBon", "execuFve",
#"compaKbility", "soRware" are all the same defect wearing different masks.
#
#Inferring that mapping was tried and abandoned. There is no reliable key, and
#a wrong guess silently rewrites the person's resume -- in testing it turned an
#email address into "nametiexample.com" and "Na>ve" into "Natfve". A
#resume is a factual claim someone will be asked about in an interview, so
#quiet corruption is worse than visible damage.
#
#What is done instead is deterministic: repair the ligatures that DO carry a
#Unicode mapping, prefer a DOCX source over a PDF whenever both exist, and
#report the rest rather than guessing at it. Measured on this corpus, DOCX
#extraction of the same document carries roughly a tenth of the artifacts.

LIGATURES = {
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl",
    "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "ft", "\ufb06": "st",
}

#lowest index wins when the same document is present in several formats
SOURCE_PREFERENCE = ['.docx', '.pdf']

#a lowercase letter, then something that cannot sit inside a word, then a
#lowercase letter. Deliberately excludes punctuation that legitimately appears
#mid-word, and runs of capitals, so product names like FSxONTAP do not count.
#excludes Markdown emphasis, which DOCX conversion legitimately produces, and
#real typography: curly quotes and en/em dashes ("Raspberry Pi\u2013based")
DAMAGE_RE = re.compile(
    r"[a-z][^a-z\sA-Z0-9.,;:()/\-'\u2018\u2019\u201c\u201d\u2013\u2014&*_\[\]]+[a-z]"
)


def normalize_extraction(text: str) -> str:
    """Expands the ligature characters that carry a real Unicode mapping.

    Lossless and unambiguous: U+FB01 is 'fi' and nothing else. No attempt is
    made to repair substitute characters, which cannot be resolved reliably.
    """
    for bad, good in LIGATURES.items():
        text = text.replace(bad, good)
    return text


#email addresses contain an "@" between letters and are not damage
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]{2,}(?:\.[\w-]{2,})+")


def extraction_damage(text: str) -> int:
    """Counts unresolvable substitute characters left in extracted text."""
    cleaned = EMAIL_RE.sub("", normalize_extraction(text))
    return len(DAMAGE_RE.findall(cleaned))


def preferred_source(source_path: Path) -> Path:
    """The best-quality source available for this document.

    A PDF is skipped when a DOCX of the same name sits beside it, because the
    DOCX extracts cleanly and the PDF usually does not.
    """
    for suffix in SOURCE_PREFERENCE:
        candidate = source_path.with_suffix(suffix)
        if candidate.is_file():
            return candidate
    return source_path


def process_file(source_path: Path):
    """Parses PDF/DOCX files into structured Markdown and updates index"""
    if source_path.suffix.lower() not in SUPPORTED_FILE_TYPES:
        return

    #a cleaner copy of the same document may sit beside this one
    best = preferred_source(source_path)
    if best != source_path:
        logger.info("Skipping %s; %s extracts more cleanly",
                    source_path.name, best.name)
        return

    logger.info("Compiling changes for: %s", source_path.name)
    try:
        result = markitdown.convert(str(source_path))
        md_filename = f"{source_path.stem}.md"
        md_path = CORPUS_DIR / md_filename

        #save output, repairing ligature damage from PDF extraction
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(normalize_extraction(result.text_content))

        #update config tracking
        update_config_index(source_path.stem, md_filename)
        logger.info("Successfully written to corpus: %s", md_filename)
    except Exception:
        logger.exception("Error processing %s", source_path.name)


def update_config_index(key: str, filename: str):
    data = load_index()
    data[key] = filename
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


def remove_from_corpus(source_path: Path):
    """Removes files from corpus if the source binary is deleted."""
    md_path = CORPUS_DIR / f"{source_path.stem}.md"
    if md_path.exists():
        md_path.unlink()

    data = load_index()
    if source_path.stem in data:
        del data[source_path.stem]
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
    logger.info("Removed %s from active corpus", source_path.stem)


#---DEBOUNCE SCHEDULER
DEBOUNCE_SECONDS = 2.0

_pending = {}
_pending_lock = threading.Lock()
_token_seq = itertools.count()


def schedule_change(source_path: Path, action):
    """Coalesces a burst of events for one file into a single deferred action.

    Every new event for a path cancels that path's previous timer, so the work
    runs once, DEBOUNCE_SECONDS after the file stops changing. That also stops
    markitdown from reading a PDF that is still being written.
    """
    token = next(_token_seq)
    timer = threading.Timer(DEBOUNCE_SECONDS, _run_pending, args=(source_path, action, token))
    timer.daemon = True
    with _pending_lock:
        previous = _pending.get(source_path)
        if previous is not None:
            previous[1].cancel()
        _pending[source_path] = (token, timer)
    timer.start()


def _run_pending(source_path: Path, action, token: int):
    with _pending_lock:
        current = _pending.get(source_path)
        if current is None or current[0] != token:
            return  #a newer event superseded this one while the timer was firing
        del _pending[source_path]
    #runs on the timer thread, so a failure here can never kill the observer
    try:
        action(source_path)
    except Exception:
        logger.exception("Failed handling %s", source_path.name)


def cancel_pending_changes():
    """Drops queued work so shutdown is not held up by debounce timers."""
    with _pending_lock:
        for _, timer in _pending.values():
            timer.cancel()
        _pending.clear()


#---WATCHDOG FILE SYSTEM MONITOR
class ResumeChangeHandler(FileSystemEventHandler):
    """Listens to the source directory and queues debounced corpus updates"""

    def _queue(self, path: Path, action):
        if path.suffix.lower() not in SUPPORTED_FILE_TYPES:
            return
        schedule_change(path, action)

    def on_created(self, event):
        if not event.is_directory:
            self._queue(Path(event.src_path), process_file)

    def on_modified(self, event):
        if not event.is_directory:
            self._queue(Path(event.src_path), process_file)

    def on_deleted(self, event):
        if not event.is_directory:
            self._queue(Path(event.src_path), remove_from_corpus)

    def on_moved(self, event):
        #Word and Pages save by writing a temp file and renaming it into place
        if event.is_directory:
            return
        self._queue(Path(event.src_path), remove_from_corpus)
        self._queue(Path(event.dest_path), process_file)


def reconcile_corpus():
    """Converts sources added or changed while the server was not running.

    A source is reprocessed only when its markdown is missing or older than the
    source, so files dropped in while the server was down get picked up without
    clobbering hand-edits made directly to the corpus.
    """
    stale = []
    seen = set()
    for file in sorted(SOURCE_DIR.iterdir()):
        if not file.is_file() or file.suffix.lower() not in SUPPORTED_FILE_TYPES:
            continue
        if file.stem in seen:
            continue
        best = preferred_source(file)
        seen.add(file.stem)
        md_path = CORPUS_DIR / f"{best.stem}.md"
        if not md_path.exists() or best.stat().st_mtime > md_path.stat().st_mtime:
            stale.append(best)

    if not stale:
        logger.info("Corpus is already up to date with %s", SOURCE_DIR.name)
        return

    logger.info("Startup reconcile: %d source file(s) out of date", len(stale))
    for file in stale:
        process_file(file)


def start_file_watcher():
    """Initializes and runs the directory sync engine thread"""

    reconcile_corpus()

    handler = ResumeChangeHandler()
    observer = Observer()
    observer.schedule(handler, path=str(SOURCE_DIR), recursive=False)
    observer.start()
    logger.info("Live watching directory: %s", SOURCE_DIR)
    return observer


@mcp.tool()
def list_available_resumes() -> str:
    """Lists variations of resumes in the corpus"""
    data = load_index()
    if not data:
        return "No resumes found. Please drop your files into the source-resumes directory"
    return "Available Resumes:\n" + "\n".join([f" - {name}" for name in data.keys()])


@mcp.tool()
def retrieve_resume(resume_name: str) -> str:
    """Retrieves full Markdown text of a specific resume variant to analyze content or alignment"""
    data = load_index()
    if not data:
        return "Error: Index not built"

    if resume_name not in data:
        return f"Resume '{resume_name}' not found. Choose from: {list(data.keys())}"

    target_md = CORPUS_DIR / data[resume_name]
    if not target_md.exists():
        return "Target text copy could not be localized on the server"

    with open(target_md, "r", encoding="utf-8") as f:
        return f.read()


def build_query_pattern(query: str) -> re.Pattern:
    """Compiles a whole-word, case-insensitive matcher for a search phrase.

    Uses \\W lookarounds rather than \\b so that queries ending or starting in
    punctuation still behave: \\b would fail on "C++", ".NET" and "CI/CD",
    because \\b asserts a word character on one side. Asserting only that the
    match is not flanked by word characters keeps "RAG" from matching
    "storage" while leaving "C++" searchable.
    """
    return re.compile(r"(?<!\w)" + re.escape(query) + r"(?!\w)", re.IGNORECASE)


@mcp.tool()
def find_resume(query_string: str) -> str:
    """Search resume names and content for a whole-word, case-insensitive phrase.

    Matches whole words, so 'RAG' finds 'RAG' and 'hybrid RAG' but not
    'storage' or 'leverage'. Punctuated terms like 'C++', '.NET' and 'CI/CD'
    work as written.
    """
    query = query_string.strip()
    if not query:
        return "Please provide a search phrase"

    data = load_index()
    if not data:
        return "Your resume corpus index is empty"

    pattern = build_query_pattern(query)
    folded = query.casefold()

    results = []
    substring_only = []
    for name, filename in data.items():
        target_md = CORPUS_DIR / filename
        if not target_md.is_file():
            continue

        with open(target_md, "r", encoding="utf-8") as f:
            lines = f.readlines()

        matching_lines = [line.strip() for line in lines if pattern.search(line)]

        if pattern.search(name) or matching_lines:
            result = f" - {name}"
            if matching_lines:
                excerpts = matching_lines[:3]
                result += "\n" + "\n".join(f"   {line}" for line in excerpts)
                if len(matching_lines) > len(excerpts):
                    result += f"\n   ... and {len(matching_lines) - len(excerpts)} more matching lines"
            results.append(result)
        elif folded in name.casefold() or any(folded in line.casefold() for line in lines):
            #matched only inside a longer word; worth reporting so an empty
            #result does not read as "nothing in the corpus"
            substring_only.append(name)

    if not results:
        if substring_only:
            return (f"Search results: no whole-word match for '{query}'. "
                    f"{len(substring_only)} variant(s) contain it inside a longer "
                    f"word (e.g. 'RAG' within 'storage'). Try a longer phrase.")
        return "Search results: No resumes match the query"

    return "Search results:\n" + "\n".join(results)



def slugify(text: str) -> str:
    """Filesystem-safe lowercase slug, for naming a draft after a job."""
    cleaned = [c.lower() if c.isalnum() else "-" for c in text.strip()]
    slug = "".join(cleaned)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:60] or "untitled"


@mcp.tool()
def draft_resume(company: str, role: str, markdown: str, job_url: str = "") -> str:
    """Saves a tailored resume draft for one job posting.

    Writes to the generated-output directory, never into the corpus. The draft is
    not searchable or retrievable until a person reviews it and runs
    'resume-mcp promote'. Use list_available_resumes and retrieve_resume first so
    the draft is built from what the person actually wrote.
    """
    if not markdown.strip():
        return "Error: markdown is empty. Nothing was written."

    output_dir = resolve_output_dir()

    #a draft written inside the corpus would become evidence for the next draft
    try:
        output_dir.relative_to(CORPUS_DIR)
    except ValueError:
        pass
    else:
        return (f"Refused: output_dir ({output_dir}) is inside the corpus "
                f"({CORPUS_DIR}). Generated drafts must stay out of the corpus.")

    jobs_dir = output_dir / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)

    today = date.today().isoformat()
    name = f"{today}-{slugify(company)}-{slugify(role)}"
    target = jobs_dir / f"{name}.md"

    #a second draft for the same role on the same day gets a suffix rather than
    #overwriting the first
    counter = 2
    while target.exists():
        target = jobs_dir / f"{name}-{counter}.md"
        counter += 1

    header = [
        f"<!-- Generated: {today} | Tailored for: {role} at {company} -->",
        "<!-- Derived output. Not corpus evidence. Never use as input to another draft. -->",
    ]
    if job_url:
        header.insert(1, f"<!-- Posting: {job_url} -->")

    target.write_text("\n".join(header) + "\n\n" + markdown.strip() + "\n",
                      encoding="utf-8")

    return (f"Draft saved to {target}\n"
            f"It is NOT in the corpus. After reading it, a person can promote it with:\n"
            f"  resume-mcp promote {target.stem} --data-dir {DATA_DIR}")


#--------PROMOTION GATE-------------
#Generated base resumes land outside the corpus. They can be promoted into it,
#but only by a person at a terminal: a resume written by a model should not
#become evidence for the next one without someone having read it first.

def load_curator_config() -> dict:
    """Reads curator.json for the generated-output location. Defaults if absent."""
    defaults = {"output_dir": DEFAULT_OUTPUT_DIR, "output_name": "base-resume"}
    path = DATA_DIR / CURATOR_FILE_NAME
    if not path.exists():
        return defaults
    try:
        with open(path, "r", encoding="utf-8") as f:
            defaults.update(json.load(f))
    except json.JSONDecodeError:
        logger.warning("%s is not valid JSON; using defaults", path)
    return defaults


def resolve_output_dir(config: dict | None = None) -> Path:
    """Generated-output directory: absolute as given, else relative to the data dir."""
    config = config if config is not None else load_curator_config()
    raw = Path(str(config.get("output_dir", DEFAULT_OUTPUT_DIR))).expanduser()
    return raw if raw.is_absolute() else (DATA_DIR / raw).resolve()


def list_generated() -> list[Path]:
    """Generated base resumes awaiting a decision, newest first."""
    output_dir = resolve_output_dir()
    if not output_dir.is_dir():
        return []
    files = [f for f in output_dir.rglob("*.md") if f.is_file()]
    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)


def promote_generated(source: Path, corpus_name: str) -> str:
    """Copies a generated resume into the corpus and indexes it.

    Callers are responsible for obtaining human approval first; this function
    performs no checks of its own beyond refusing to shadow a tracked source.
    """
    if (CORPUS_DIR / f"{corpus_name}.md").exists():
        return f"Refused: '{corpus_name}' already exists in the corpus."

    #a source binary with this stem would have its markdown regenerated on the
    #next reconcile, silently discarding whatever was promoted here
    for suffix in SUPPORTED_FILE_TYPES:
        if (SOURCE_DIR / f"{corpus_name}{suffix}").exists():
            return (f"Refused: source-resumes/{corpus_name}{suffix} exists, and "
                    f"reconcile would overwrite the promoted copy. Pick another name.")

    shutil.copy2(source, CORPUS_DIR / f"{corpus_name}.md")
    update_config_index(corpus_name, f"{corpus_name}.md")
    return f"Promoted '{corpus_name}' into the corpus."


def cmd_review(args) -> int:
    """Shows generated base resumes and whether each is in the corpus yet."""
    configure(resolve_data_dir(args.data_dir), create=False)
    generated = list_generated()
    if not generated:
        print(f"No generated resumes in {resolve_output_dir()}")
        return 0

    index = load_index()
    promoted = set(index.values())
    print(f"Generated resumes in {resolve_output_dir()}:\n")
    output_dir = resolve_output_dir()
    for f in generated:
        state = "in corpus" if f.name in promoted else "not in corpus"
        modified = date.fromtimestamp(f.stat().st_mtime).isoformat()
        print(f"  {f.relative_to(output_dir)}")
        print(f"    {state}, modified {modified}")
    print("\nPromote one with:  resume-mcp promote <name>")
    print("Promotion is interactive by design -- a person reads it first.")
    return 0


def cmd_promote(args) -> int:
    """Human-in-the-loop promotion of one generated resume into the corpus."""
    configure(resolve_data_dir(args.data_dir), create=False)

    candidate = Path(args.name).expanduser()
    if not candidate.is_file():
        stem = Path(args.name).stem
        matches = [f for f in list_generated() if f.stem == stem]
        if len(matches) > 1:
            print(f"Ambiguous: {len(matches)} drafts named '{stem}'. Pass a full path:",
                  file=sys.stderr)
            for m in matches:
                print(f"  {m}", file=sys.stderr)
            return 1
        candidate = matches[0] if matches else resolve_output_dir() / f"{stem}.md"
    if not candidate.is_file():
        print(f"Not found: {candidate}", file=sys.stderr)
        print(f"Run 'resume-mcp review --data-dir {DATA_DIR}' to see what is available.",
              file=sys.stderr)
        return 1

    #the gate. a cron job, a headless agent, or a piped script has no person in
    #it, and this step exists precisely to require one.
    if not sys.stdin.isatty():
        print("Refused: promotion requires an interactive terminal.", file=sys.stderr)
        print("A generated resume should be read by a person before it becomes "
              "evidence for the next one.", file=sys.stderr)
        return 2

    text = candidate.read_text(encoding="utf-8")
    corpus_name = args.as_name or candidate.stem

    print("=" * 72)
    print(text)
    print("=" * 72)
    print(f"\nSource:       {candidate}")
    print(f"Promote into: {CORPUS_DIR}/{corpus_name}.md")
    print("\nOnce promoted this becomes corpus evidence: searchable, retrievable,")
    print("and available as source material the next time a resume is generated.")

    answer = input(f"\nPromote '{corpus_name}' into the corpus? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("Not promoted.")
        return 0

    print(promote_generated(candidate, corpus_name))
    return 0


#--------INITIALIZATION CONTROLLER-------------
def cmd_normalize(args) -> int:
    """Repairs ligature damage in corpus files already on disk.

    Reports by default; only rewrites with --apply. Conversions from here on
    are repaired automatically, so this is for a corpus built before that.
    """
    configure(resolve_data_dir(args.data_dir), create=False)

    index = load_index()
    if not index:
        print("Corpus index is empty; nothing to normalize.")
        return 0

    damaged = []
    for name, filename in sorted(index.items()):
        path = CORPUS_DIR / filename
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        count = extraction_damage(text)
        if count:
            damaged.append((name, path, text, count))

    if not damaged:
        print(f"No extraction damage found across {len(index)} corpus files.")
        return 0

    total = sum(c for _, _, _, c in damaged)
    print(f"{len(damaged)} of {len(index)} corpus files carry extraction damage "
          f"({total} artifacts):\n")
    for name, _, text, count in damaged:
        fixed = normalize_extraction(text)
        sample = next(
            (f"{b.strip()}  ->  {a.strip()}"
             for b, a in zip(text.splitlines(), fixed.splitlines())
             if b != a), "")
        print(f"  {name}  ({count})")
        if sample:
            print(f"      {sample[:150]}")

    if not args.apply:
        print("\nThis was a report. Re-run with --apply to rewrite these files.")
        print("Corpus files are regenerated from source-resumes/ only when the")
        print("source is newer, so repaired text will not be clobbered.")
        return 0

    for name, path, text, _ in damaged:
        path.write_text(normalize_extraction(text), encoding="utf-8")
    print(f"\nRepaired {len(damaged)} file(s).")
    return 0


def run_subcommand(name: str, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"resume-mcp {name}")
    parser.add_argument("--data-dir", help="Corpus location. Defaults to "
                                           "$RESUME_MCP_DATA_DIR, then the working directory.")
    if name == "promote":
        parser.add_argument("name", help="Generated resume to promote: a name or a path.")
        parser.add_argument("--as", dest="as_name",
                            help="Corpus name to promote it under. Defaults to the filename stem.")
    if name == "normalize":
        parser.add_argument("--apply", action="store_true",
                            help="Rewrite the damaged files. Without this, only reports.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    handlers = {"promote": cmd_promote, "review": cmd_review, "normalize": cmd_normalize}
    return handlers[name](args)


SUBCOMMANDS = ("review", "promote", "normalize")


def main():
    #bare invocation runs the MCP server, so an existing MCP client config
    #keeps working unchanged; subcommands are opt-in by name
    if len(sys.argv) > 1 and sys.argv[1] in SUBCOMMANDS:
        return run_subcommand(sys.argv[1], sys.argv[2:])

    parser = argparse.ArgumentParser(
        prog="resume-mcp",
        description="MCP server that keeps a searchable Markdown corpus of your resumes. "
                    "Subcommands: review, promote.",
    )
    parser.add_argument(
        "--data-dir",
        help="Folder holding source-resumes/ and markdown-resumes/. "
             "Defaults to $RESUME_MCP_DATA_DIR, then the working directory.",
    )
    parser.add_argument(
        "--no-watch",
        action="store_true",
        help="Reconcile once at startup, then skip the live file watcher.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    configure(resolve_data_dir(args.data_dir))
    logger.info("Launching resume corpus manager as %s", mcp.name)
    logger.info("Scanning files from %s", DATA_DIR)

    watcher = None
    if args.no_watch:
        reconcile_corpus()
    else:
        watcher = start_file_watcher()

    try:
        #Launch FastMCP over standard I/O transport loop
        mcp.run()
    finally:
        cancel_pending_changes()
        if watcher is not None:
            watcher.stop()
            watcher.join()


if __name__ == "__main__":
    sys.exit(main() or 0)
