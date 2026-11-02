import argparse
import collections
import contextlib
import difflib
import itertools
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import date, datetime
import threading
from pathlib import Path

try:
    import fcntl
except ImportError:  #Windows: the thread lock still applies
    fcntl = None

from fastmcp import FastMCP
import pypdfium2
from markitdown import MarkItDown
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

SUPPORTED_FILE_TYPES = ['.pdf', '.docx']
DEFAULT_SERVER_NAME = "Resume Corpus Manager"
CURATOR_FILE_NAME = "curator.json"
DEFAULT_OUTPUT_DIR = "base-resumes"
DEFAULT_PDF_SUBDIR = "pdf"
HISTORY_DIR_NAME = "corpus-history"

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


class IndexUnreadableError(Exception):
    """The index exists but cannot be parsed, so it must not be overwritten."""


def load_index(strict: bool = False) -> dict:
    """Reads the name -> markdown-filename index.

    Readers get {} for a missing or unreadable file. Writers pass strict=True:
    treating an unreadable index as empty and then saving would replace the
    whole corpus with one entry, which is exactly how an index gets wiped.
    """
    if CONFIG_FILE is None or not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        if strict:
            raise IndexUnreadableError(f"{CONFIG_FILE} is not valid JSON") from e
        logger.warning("Index at %s is not valid JSON; treating it as empty", CONFIG_FILE)
        return {}


#Watchdog timers run on separate threads, and Claude Desktop and Claude Code
#may each run their own server process against the same data dir, so updates
#need both a thread lock and a file lock.
_index_thread_lock = threading.RLock()


@contextlib.contextmanager
def index_lock():
    """Serializes read-modify-write of the index across threads and processes."""
    with _index_thread_lock:
        lock_path = CONFIG_FILE.with_name(CONFIG_FILE.name + ".lock")
        with open(lock_path, "a") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)


def save_index(data: dict):
    """Writes the index atomically, so a reader never sees a half-written file."""
    tmp = CONFIG_FILE.with_name(f".{CONFIG_FILE.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_FILE)


def modify_index(change) -> bool:
    """Applies change(data) to the index under lock. Returns False if refused."""
    with index_lock():
        try:
            data = load_index(strict=True)
        except IndexUnreadableError:
            logger.error("Index at %s is unreadable; leaving it untouched. "
                         "Fix or delete it, then restart the server.", CONFIG_FILE)
            return False
        if change(data) is not False:
            save_index(data)
        return True



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
    #U+FFFE is a permanent non-character and never valid in text. These PDFs
    #carry it where a hyphen was broken across a line: "self<FFFE>paced",
    #"circuit<FFFE>switched". A plain hyphen is the only sensible reading, and
    #leaving an invalid code point in the corpus is strictly worse.
    "\ufffe": "-",
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


def convert_document(source_path: Path) -> str:
    """Extracts text, choosing the extractor by format.

    PDFs go through pypdfium2 rather than markitdown. On this kind of document
    -- Pages-exported PDFs with embedded fonts -- markitdown's PDF path
    mis-decodes glyphs wholesale, yielding "plaGorm", "automaKon", "soRware",
    while pypdfium2 reads the same files correctly. Measured across five of
    them: 130-149 artifacts via markitdown, 17-23 via pypdfium2, and the
    remainder are product names like TypeScript, not damage.

    DOCX stays on markitdown, which is accurate there and adds useful
    structure (real emphasis and links) that raw text extraction cannot give.
    """
    if source_path.suffix.lower() == ".pdf":
        pdf = pypdfium2.PdfDocument(str(source_path))
        try:
            return "\n".join(
                page.get_textpage().get_text_range() for page in pdf
            )
        finally:
            pdf.close()
    return markitdown.convert(str(source_path)).text_content


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
        result = convert_document(source_path)
        md_filename = f"{source_path.stem}.md"
        md_path = CORPUS_DIR / md_filename

        #save output, repairing ligature damage from PDF extraction
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(normalize_extraction(result))

        #update config tracking
        if update_config_index(source_path.stem, md_filename):
            logger.info("Successfully written to corpus: %s", md_filename)
    except Exception:
        logger.exception("Error processing %s", source_path.name)


def update_config_index(key: str, filename: str) -> bool:
    return modify_index(lambda data: data.__setitem__(key, filename))


def remove_from_corpus(source_path: Path):
    """Removes files from corpus if the source binary is deleted."""
    md_path = CORPUS_DIR / f"{source_path.stem}.md"
    if md_path.exists():
        md_path.unlink()

    def drop(data):
        if source_path.stem not in data:
            return False
        del data[source_path.stem]

    modify_index(drop)
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
    unindexed = []
    seen = set()
    index = load_index()
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
        elif best.stem not in index:
            #converted earlier but dropped from the index; without this, an
            #index that lost entries would never recover them
            unindexed.append(best.stem)

    if unindexed:
        def restore(data):
            for stem in unindexed:
                data.setdefault(stem, f"{stem}.md")

        if modify_index(restore):
            logger.info("Startup reconcile: re-indexed %d resume(s) missing from %s",
                        len(unindexed), CONFIG_FILE.name)

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
            f"It is NOT in the corpus. A person can preview it as a PDF, then promote it:\n"
            f"  resume-mcp render {target.stem} --data-dir {DATA_DIR}\n"
            f"  resume-mcp promote {target.stem} --data-dir {DATA_DIR}")


#--------CURATOR CONFIG-------------
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
    """Generated drafts awaiting a decision, newest first."""
    output_dir = resolve_output_dir()
    if not output_dir.is_dir():
        return []
    files = [f for f in output_dir.rglob("*.md") if f.is_file()]
    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)


def _resolve_configured(raw) -> Path:
    path = Path(str(raw)).expanduser()
    return path if path.is_absolute() else (DATA_DIR / path).resolve()


def resolve_pdf_dir(config: dict | None = None) -> Path:
    """Where PDFs go: curator.json's pdf_dir, else <output_dir>/pdf.

    Kept apart from the Markdown drafts so each folder holds one kind of file.
    """
    config = config if config is not None else load_curator_config()
    if config.get("pdf_dir"):
        return _resolve_configured(config["pdf_dir"])
    return resolve_output_dir(config) / DEFAULT_PDF_SUBDIR


def resolve_style_dir(config: dict | None = None) -> Path:
    """Where resume.lua and resume.css live: curator.json's style_dir, else <data>/styles."""
    config = config if config is not None else load_curator_config()
    return _resolve_configured(config.get("style_dir") or "styles")


#--------PROVENANCE-------------
#A promoted copy starts with one comment naming the draft it came from, so
#'redo' knows where to look and 'review' can tell when a draft has been edited
#since. Pandoc drops HTML comments, so it never shows up in a PDF.
PROVENANCE_RE = re.compile(r"\A<!-- Promoted: (\S+) \| From: (.+?) -->\n")


def split_provenance(text: str) -> tuple[dict | None, str]:
    """Separates a corpus copy's provenance line from the resume itself."""
    m = PROVENANCE_RE.match(text)
    if not m:
        return None, text
    return {"date": m.group(1), "source": Path(m.group(2))}, text[m.end():]


def with_provenance(text: str, source: Path) -> str:
    _, body = split_provenance(text)  #never stack two provenance lines
    return f"<!-- Promoted: {date.today().isoformat()} | From: {source.resolve()} -->\n{body}"


def promoted_sources() -> dict[Path, str]:
    """Maps each draft path to the corpus name it was promoted as."""
    found = {}
    for name, filename in load_index().items():
        path = CORPUS_DIR / filename
        if not path.is_file():
            continue
        with open(path, "r", encoding="utf-8") as f:
            meta, _ = split_provenance(f.readline())
        if meta:
            found[meta["source"]] = name
    return found


#--------NAME MATCHING-------------
def _tokens(query: str) -> list[str]:
    return [t for t in re.split(r"[-_\s]+", query.casefold()) if t]


def _token_match(query: str, candidates: list[str]) -> list[str]:
    """Exact match wins; otherwise every word of the query must appear.

    So 'acme staff' finds '2026-11-02-acme-staff-platform-engineer'.
    """
    if query in candidates:
        return [query]
    tokens = _tokens(query)
    if not tokens:
        return []
    return [c for c in candidates if all(t in c.casefold() for t in tokens)]


def match_drafts(query: str) -> list[Path]:
    path = Path(query).expanduser()
    if path.is_file():
        return [path.resolve()]
    drafts = list_generated()
    by_stem = collections.defaultdict(list)
    for d in drafts:
        by_stem[d.stem].append(d)
    stems = _token_match(Path(query).stem if query.endswith(".md") else query, list(by_stem))
    return [d for s in stems for d in by_stem[s]]


def match_corpus(query: str) -> list[str]:
    return _token_match(query, list(load_index()))


def _report_matches(kind: str, query: str, matches: list) -> bool:
    """Prints a not-found or ambiguous message. True when exactly one matched."""
    if len(matches) == 1:
        return True
    if not matches:
        print(f"No {kind} matches '{query}'.", file=sys.stderr)
        print(f"Run 'resume-mcp review --data-dir {DATA_DIR}' to see what is available.",
              file=sys.stderr)
    else:
        print(f"'{query}' matches {len(matches)} {kind}s. Be more specific:", file=sys.stderr)
        for m in matches:
            print(f"  {m}", file=sys.stderr)
    return False


#--------PDF RENDERING-------------
class RenderError(Exception):
    pass


def render_pdf(markdown_path: Path, pdf_name: str) -> Path:
    """Renders Markdown to a styled PDF in the PDF folder with pandoc + weasyprint."""
    pandoc = shutil.which("pandoc")
    if not pandoc:
        raise RenderError("pandoc is not installed (brew install pandoc)")
    if not shutil.which("weasyprint"):
        raise RenderError("weasyprint is not installed (brew install weasyprint)")

    #resume.css is required; resume.lua is optional. A filter lets a style
    #rebuild structure (headings, role lines) that plain Markdown lacks, but a
    #stylesheet alone is enough to style pandoc's default HTML.
    style_dir = resolve_style_dir()
    lua, css = style_dir / "resume.lua", style_dir / "resume.css"
    if not css.is_file():
        raise RenderError(f"resume.css not found in {style_dir}. Set style_dir in "
                          f"{CURATOR_FILE_NAME}; see 'Styling PDFs' in the README.")

    pdf_dir = resolve_pdf_dir()
    pdf_dir.mkdir(parents=True, exist_ok=True)
    out = pdf_dir / f"{pdf_name}.pdf"
    #plain-style drafts put "- " bullets directly under a role line
    command = [pandoc, "-f", "markdown+lists_without_preceding_blankline",
               str(markdown_path), "-o", str(out),
               "--pdf-engine=weasyprint", f"--css={css}"]
    if lua.is_file():
        command.append(f"--lua-filter={lua}")
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RenderError(result.stderr.strip()[-800:] or "pandoc failed")
    return out


def _render_and_report(markdown_path: Path, pdf_name: str) -> bool:
    try:
        out = render_pdf(markdown_path, pdf_name)
    except RenderError as e:
        print(f"PDF not created: {e}", file=sys.stderr)
        print(f"Retry with:  resume-mcp render {pdf_name} --data-dir {DATA_DIR}",
              file=sys.stderr)
        return False
    print(f"PDF: {out}")
    return True


#--------PROMOTION GATE-------------
#Generated drafts land outside the corpus. They can be promoted into it, or
#replace an earlier promotion, but only by a person at a terminal: a resume
#written by a model should not become evidence for the next one without
#someone having read it first.

def _write_atomic(path: Path, text: str):
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def corpus_name_problem(corpus_name: str) -> str | None:
    """Why a new corpus name cannot be used, or None if it can."""
    if not corpus_name or "/" in corpus_name or corpus_name.startswith("."):
        return f"'{corpus_name}' is not a usable file name."
    if (CORPUS_DIR / f"{corpus_name}.md").exists() or corpus_name in load_index():
        return (f"'{corpus_name}' is already in the corpus. To replace it, run:\n"
                f"  resume-mcp redo {corpus_name}")
    #a source binary with this stem would have its markdown regenerated on the
    #next reconcile, silently discarding whatever was promoted here
    for suffix in SUPPORTED_FILE_TYPES:
        if (SOURCE_DIR / f"{corpus_name}{suffix}").exists():
            return (f"source-resumes/{corpus_name}{suffix} exists, and reconcile would "
                    f"overwrite the promoted copy. Pick another name.")
    return None


def promote_generated(source: Path, corpus_name: str) -> str:
    """Copies a generated resume into the corpus and indexes it.

    Callers are responsible for obtaining human approval first.
    """
    problem = corpus_name_problem(corpus_name)
    if problem:
        return f"Refused: {problem}"

    target = CORPUS_DIR / f"{corpus_name}.md"
    _write_atomic(target, with_provenance(source.read_text(encoding="utf-8"), source))
    if not update_config_index(corpus_name, target.name):
        target.unlink()  #undo the copy so promoting again is not refused
        return (f"Not promoted: {CONFIG_FILE.name} is unreadable. "
                f"Fix it, then promote again.")
    return f"Promoted '{corpus_name}' into the corpus."


def redo_problem(corpus_name: str) -> str | None:
    """Why a corpus entry cannot be replaced by a draft, or None if it can."""
    if corpus_name not in load_index():
        return f"'{corpus_name}' is not in the corpus. Use 'resume-mcp promote' instead."
    for suffix in SUPPORTED_FILE_TYPES:
        if (SOURCE_DIR / f"{corpus_name}{suffix}").exists():
            return (f"'{corpus_name}' is converted from source-resumes/{corpus_name}{suffix}. "
                    f"Update that file instead; the server reconverts it.")
    return None


def redo_promoted(corpus_name: str, source: Path) -> str:
    """Replaces a promoted corpus copy with a newer draft, keeping the old one.

    Callers are responsible for obtaining human approval first.
    """
    problem = redo_problem(corpus_name)
    if problem:
        return f"Refused: {problem}"

    target = CORPUS_DIR / load_index()[corpus_name]
    if target.is_file():
        #outside markdown-resumes/, so old versions are never indexed or searched
        history = DATA_DIR / HISTORY_DIR_NAME
        history.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        shutil.copy2(target, history / f"{stamp}-{target.name}")

    _write_atomic(target, with_provenance(source.read_text(encoding="utf-8"), source))
    return f"Replaced '{corpus_name}' in the corpus. The previous version is in {HISTORY_DIR_NAME}/."


def _require_terminal(action: str) -> bool:
    #the gate. a cron job, a headless agent, or a piped script has no person in
    #it, and this step exists precisely to require one.
    if sys.stdin.isatty():
        return True
    print(f"Refused: {action} requires an interactive terminal.", file=sys.stderr)
    print("A generated resume should be read by a person before it becomes "
          "evidence for the next one.", file=sys.stderr)
    return False


def cmd_review(args) -> int:
    """Shows generated drafts and whether each is in the corpus yet."""
    configure(resolve_data_dir(args.data_dir), create=False)
    output_dir = resolve_output_dir()
    generated = list_generated()
    if not generated:
        print(f"No generated resumes in {output_dir}")
        return 0

    index = load_index()
    sources = promoted_sources()
    print(f"Generated resumes in {output_dir}:\n")
    for f in generated:
        modified = date.fromtimestamp(f.stat().st_mtime).isoformat()
        corpus_name = sources.get(f.resolve())
        if corpus_name:
            _, body = split_provenance((CORPUS_DIR / index[corpus_name]).read_text(encoding="utf-8"))
            if body == f.read_text(encoding="utf-8"):
                state = f"in corpus as {corpus_name}"
            else:
                state = (f"in corpus as {corpus_name}, but the draft has changed since. "
                         f"Update with: resume-mcp redo {corpus_name}")
        elif f.name in index.values():
            state = "in corpus"
        else:
            state = "not in corpus"
        print(f"  {f.relative_to(output_dir)}")
        print(f"    {state} (modified {modified})")
    print(f"\nPDFs go to {resolve_pdf_dir()}")
    print("Preview:  resume-mcp render <draft>")
    print("Promote:  resume-mcp promote <draft>      Replace:  resume-mcp redo <corpus name>")
    print("Promotion is interactive by design -- a person reads it first.")
    return 0


def cmd_promote(args) -> int:
    """Human-in-the-loop promotion of one generated resume into the corpus."""
    configure(resolve_data_dir(args.data_dir), create=False)

    matches = match_drafts(args.name)
    if not _report_matches("draft", args.name, matches):
        return 1
    candidate = matches[0]

    if args.as_name:
        problem = corpus_name_problem(args.as_name)
        if problem:
            print(f"Refused: {problem}", file=sys.stderr)
            return 1

    if not _require_terminal("promotion"):
        return 2

    print("=" * 72)
    print(candidate.read_text(encoding="utf-8"))
    print("=" * 72)
    print(f"\nSource: {candidate}")

    corpus_name = args.as_name
    while not corpus_name:
        entered = input(f"\nCorpus name [{candidate.stem}]: ").strip() or candidate.stem
        problem = corpus_name_problem(entered)
        if problem:
            print(problem)
        else:
            corpus_name = entered

    print(f"\nPromote into: {CORPUS_DIR}/{corpus_name}.md")
    print("Once promoted this becomes corpus evidence: searchable, retrievable,")
    print("and available as source material the next time a resume is generated.")
    answer = input(f"\nPromote '{corpus_name}' into the corpus? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("Not promoted.")
        return 0

    result = promote_generated(candidate, corpus_name)
    print(result)
    if not result.startswith("Promoted"):
        return 1
    if not args.no_pdf:
        _render_and_report(CORPUS_DIR / f"{corpus_name}.md", corpus_name)
    return 0


def cmd_redo(args) -> int:
    """Human-in-the-loop replacement of a promoted resume with a revised draft."""
    configure(resolve_data_dir(args.data_dir), create=False)

    matches = match_corpus(args.name)
    if not _report_matches("corpus resume", args.name, matches):
        return 1
    corpus_name = matches[0]
    problem = redo_problem(corpus_name)
    if problem:
        print(f"Refused: {problem}", file=sys.stderr)
        return 1

    target = CORPUS_DIR / load_index()[corpus_name]
    current = target.read_text(encoding="utf-8") if target.is_file() else ""
    meta, current_body = split_provenance(current)

    if args.draft:
        drafts = match_drafts(args.draft)
        if not _report_matches("draft", args.draft, drafts):
            return 1
        draft = drafts[0]
    elif meta and meta["source"].is_file():
        draft = meta["source"]
    else:
        where = f" ({meta['source']} no longer exists)" if meta else ""
        print(f"Don't know which draft to use for '{corpus_name}'{where}. Name it:",
              file=sys.stderr)
        print(f"  resume-mcp redo {corpus_name} --with <draft>", file=sys.stderr)
        return 1

    new_text = draft.read_text(encoding="utf-8")
    if new_text == current_body:
        print(f"'{corpus_name}' already matches {draft.name}; the corpus is unchanged.")
        if not args.no_pdf:
            _render_and_report(target, corpus_name)
        return 0

    if not _require_terminal("redo"):
        return 2

    diff = difflib.unified_diff(
        current_body.splitlines(), new_text.splitlines(),
        fromfile=f"corpus: {corpus_name}", tofile=f"draft: {draft.name}", lineterm="",
    )
    print("=" * 72)
    print("\n".join(diff))
    print("=" * 72)
    print(f"\nReplace: {target}")
    print(f"With:    {draft}")
    print(f"The current version is kept in {DATA_DIR / HISTORY_DIR_NAME}/.")
    answer = input(f"\nReplace '{corpus_name}' with this draft? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("Not replaced.")
        return 0

    result = redo_promoted(corpus_name, draft)
    print(result)
    if not result.startswith("Replaced"):
        return 1
    if not args.no_pdf:
        _render_and_report(target, corpus_name)
    return 0


def cmd_render(args) -> int:
    """Renders a promoted resume or a draft to a styled PDF. Changes no corpus files."""
    configure(resolve_data_dir(args.data_dir), create=False)
    index = load_index()

    #a promoted resume renders from its corpus copy, so the PDF always matches
    #what was approved; a draft renders as a preview before promotion
    #one matching promoted resume wins; add words to reach a draft instead
    #("acme staff" -> the promoted copy, "acme staff v2" -> the v2 draft)
    corpus = match_corpus(args.name)
    drafts = match_drafts(args.name)
    exact_draft = [d for d in drafts if d.stem == Path(args.name).stem
                   or d == Path(args.name).expanduser().resolve()]
    if exact_draft:
        corpus, drafts = [], exact_draft
    elif corpus:
        drafts = []

    choices = [f"corpus: {c}" for c in corpus] + [f"draft:  {d}" for d in drafts]
    if not _report_matches("resume", args.name, choices):
        return 1
    if corpus:
        ok = _render_and_report(CORPUS_DIR / index[corpus[0]], corpus[0])
    else:
        ok = _render_and_report(drafts[0], drafts[0].stem)
    return 0 if ok else 1


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
        parser.add_argument("name", nargs="+", help="Draft to promote: a path, a name, or "
                                                    "words from its name (acme staff v2).")
        parser.add_argument("--as", dest="as_name",
                            help="Corpus name to promote it under. Asked for if omitted.")
    if name == "redo":
        parser.add_argument("name", nargs="+", help="Promoted resume to replace: its corpus "
                                                    "name, or words from it.")
        parser.add_argument("--with", dest="draft", nargs="+",
                            help="Revised draft to replace it with. Defaults to the draft "
                                 "it was promoted from.")
    if name == "render":
        parser.add_argument("name", nargs="+", help="Corpus resume or draft to render.")
    if name in ("promote", "redo"):
        parser.add_argument("--no-pdf", action="store_true", help="Skip making the PDF.")
    if name == "normalize":
        parser.add_argument("--apply", action="store_true",
                            help="Rewrite the damaged files. Without this, only reports.")
    args = parser.parse_args(argv)
    for field in ("name", "draft"):
        if isinstance(getattr(args, field, None), list):
            setattr(args, field, " ".join(getattr(args, field)))

    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    handlers = {"promote": cmd_promote, "review": cmd_review, "normalize": cmd_normalize,
                "redo": cmd_redo, "render": cmd_render}
    return handlers[name](args)


SUBCOMMANDS = ("review", "promote", "redo", "render", "normalize")


def main():
    #bare invocation runs the MCP server, so an existing MCP client config
    #keeps working unchanged; subcommands are opt-in by name
    if len(sys.argv) > 1 and sys.argv[1] in SUBCOMMANDS:
        return run_subcommand(sys.argv[1], sys.argv[2:])

    parser = argparse.ArgumentParser(
        prog="resume-mcp",
        description="MCP server that keeps a searchable Markdown corpus of your resumes. "
                    "Subcommands: review, promote, redo, render, normalize.",
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
