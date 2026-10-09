import argparse
import itertools
import json
import logging
import os
import threading
from pathlib import Path

from fastmcp import FastMCP
from markitdown import MarkItDown
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

SUPPORTED_FILE_TYPES = ['.pdf', '.docx']
DEFAULT_SERVER_NAME = "Resume Corpus Manager"

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


def process_file(source_path: Path):
    """Parses PDF/DOCX files into structured Markdown and updates index"""
    if source_path.suffix.lower() not in SUPPORTED_FILE_TYPES:
        return

    logger.info("Compiling changes for: %s", source_path.name)
    try:
        result = markitdown.convert(str(source_path))
        md_filename = f"{source_path.stem}.md"
        md_path = CORPUS_DIR / md_filename

        #save output
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(result.text_content)

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
    for file in sorted(SOURCE_DIR.iterdir()):
        if not file.is_file() or file.suffix.lower() not in SUPPORTED_FILE_TYPES:
            continue
        md_path = CORPUS_DIR / f"{file.stem}.md"
        if not md_path.exists() or file.stat().st_mtime > md_path.stat().st_mtime:
            stale.append(file)

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


@mcp.tool()
def find_resume(query_string: str) -> str:
    """Search resume names and Markdown content for a case-insensitive phrase."""
    query = query_string.strip().casefold()
    if not query:
        return "Please provide a search phrase"

    data = load_index()
    if not data:
        return "Your resume corpus index is empty"

    results = []
    for name, filename in data.items():
        target_md = CORPUS_DIR / filename
        if not target_md.is_file():
            continue

        with open(target_md, "r", encoding="utf-8") as f:
            matching_lines = [
                line.strip()
                for line in f
                if query in line.casefold()
            ]

        if query in name.casefold() or matching_lines:
            result = f" - {name}"
            if matching_lines:
                excerpts = matching_lines[:3]
                result += "\n" + "\n".join(f"   {line}" for line in excerpts)
                if len(matching_lines) > len(excerpts):
                    result += f"\n   ... and {len(matching_lines) - len(excerpts)} more matching lines"
            results.append(result)

    if not results:
        return "Search results: No resumes match the query"

    return "Search results:\n" + "\n".join(results)


#--------INITIALIZATION CONTROLLER-------------
def main():
    parser = argparse.ArgumentParser(
        prog="resume-mcp",
        description="MCP server that keeps a searchable Markdown corpus of your resumes.",
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
    main()
