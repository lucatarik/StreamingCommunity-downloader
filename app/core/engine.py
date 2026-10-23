"""Bridge to `sc-dl-engine`, the Rust download engine.

The engine runs as a **child process**, and that is the point of it rather than
an implementation detail. A blocking network call cannot be killed from outside
its thread in Python, so the old in-process downloader could only *ask* an
attempt to stop: `cancel_event.set()` and hope. An attempt wedged on a socket
kept running, and would later write its stale result over a newer attempt's —
which is why `jobs.py` grew generation counters, per-generation temp folders and
"am I still the current attempt?" guards on every write.

A child process can be killed. `cancel()` below sends SIGTERM, and escalates to
SIGKILL if the engine does not stop, so by the time this function returns the
old download is provably over. That guarantee is what let all of the above be
deleted.
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import Optional

from app.progress import DownloadCancelledError

logger = logging.getLogger(__name__)

# Exit codes, mirroring `engine/src/spec.rs`.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BAD_SPEC = 2
EXIT_CANCELLED = 130

# How long the engine gets to wind down after SIGTERM before it is killed
# outright. It cancels at await points, so this is generous; the escalation
# exists so a hang in code nobody anticipated still cannot outlive a cancel.
TERM_GRACE_SECONDS = 10

_MISSING_BINARY_HINT = (
    "Motore di download non trovato. Compilalo con "
    "`cargo build --release --manifest-path engine/Cargo.toml` "
    "oppure imposta SC_ENGINE_BIN sul percorso del binario."
)


def engine_path() -> str:
    """Locate the engine binary.

    Checked in order: an explicit override, the PATH (how the Docker image
    ships it), then a local cargo build tree (how a developer runs it).
    """
    override = os.getenv("SC_ENGINE_BIN")
    if override:
        return override

    found = shutil.which("sc-dl-engine")
    if found:
        return found

    repo_root = Path(__file__).resolve().parents[2]
    for profile in ("release", "debug"):
        candidate = repo_root / "engine" / "target" / profile / "sc-dl-engine"
        if candidate.exists():
            return str(candidate)

    raise RuntimeError(_MISSING_BINARY_HINT)


class _EngineProcess:
    """One engine run, with cancellation wired to a `threading.Event`."""

    def __init__(self, spec: dict, cancel_event: Optional[threading.Event]):
        self._spec = spec
        self._cancel_event = cancel_event
        self._proc: Optional[subprocess.Popen] = None
        self._watcher: Optional[threading.Thread] = None
        self._stop_watching = threading.Event()

    def _start_cancel_watcher(self):
        if self._cancel_event is None:
            return

        def watch():
            while not self._stop_watching.is_set():
                if self._cancel_event.wait(timeout=0.5):
                    self._terminate()
                    return

        self._watcher = threading.Thread(
            target=watch, name="engine-cancel-watch", daemon=True
        )
        self._watcher.start()

    def _terminate(self):
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        logger.info("Cancellazione richiesta: SIGTERM al motore (pid %s)", proc.pid)
        try:
            proc.terminate()
        except OSError:
            return
        try:
            proc.wait(timeout=TERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            logger.warning(
                "Il motore (pid %s) non si è fermato entro %ss: SIGKILL",
                proc.pid, TERM_GRACE_SECONDS,
            )
            try:
                proc.kill()
            except OSError:
                pass

    def _drain_stderr(self, stream):
        """Forward the engine's logs into ours.

        This has to run concurrently with reading stdout: an engine that fills
        its stderr pipe while nobody reads it blocks forever, and would look
        exactly like the stall this whole rewrite set out to remove.
        """
        for raw in stream:
            line = raw.rstrip()
            if line:
                logger.info("[engine] %s", line)

    def run(self, on_event) -> str:
        binary = engine_path()
        logger.info("Avvio motore: %s", binary)

        try:
            self._proc = subprocess.Popen(
                [binary],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                # A new process group, so a signal aimed at the panel does not
                # also hit a download that is meant to survive it.
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(_MISSING_BINARY_HINT) from exc

        stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(self._proc.stderr,),
            name="engine-stderr", daemon=True,
        )
        stderr_thread.start()

        # Cancellation is armed before the spec is written: a cancel that
        # arrives in that window must still be honoured.
        self._start_cancel_watcher()

        try:
            self._proc.stdin.write(json.dumps(self._spec))
            self._proc.stdin.close()
        except (BrokenPipeError, OSError) as exc:
            self._terminate()
            raise RuntimeError(f"Il motore non ha accettato il job: {exc}") from exc

        output_path = None
        error_message = None
        cancelled = False

        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Evento non leggibile dal motore: %r", line)
                continue

            kind = event.get("type")
            if kind == "done":
                output_path = event.get("output_path")
            elif kind == "error":
                error_message = event.get("message")
            elif kind == "cancelled":
                cancelled = True
            else:
                on_event(event)

        code = self._proc.wait()
        self._stop_watching.set()
        stderr_thread.join(timeout=5)

        if cancelled or code == EXIT_CANCELLED:
            raise DownloadCancelledError("Download annullato dall'utente")

        if code == EXIT_OK and output_path:
            return output_path

        # A cancel that landed between the last event and the exit still means
        # cancelled, not failed — the difference drives what the panel offers
        # the user next.
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise DownloadCancelledError("Download annullato dall'utente")

        raise RuntimeError(error_message or f"Il motore è terminato con codice {code}")


def run_download(
    *,
    m3u8_index: str,
    output_path: str,
    temp_dir: str,
    key: Optional[str] = None,
    referer: Optional[str] = None,
    audio_track_urls: Optional[list] = None,
    subtitle_track_urls: Optional[list] = None,
    subtitle_languages: Optional[list] = None,
    subtitle_mode: str = "embed",
    max_segment_workers: int = 16,
    user_agent: Optional[str] = None,
    ffmpeg_path: Optional[str] = None,
    progress_factory=None,
    cancel_event: Optional[threading.Event] = None,
) -> str:
    """Download one title. Returns the path actually written.

    Raises `DownloadCancelledError` on cancellation and `RuntimeError` on
    failure — the same contract the in-process downloader had, so `jobs.py` and
    the request system did not have to change to accommodate it.
    """
    spec = {
        "m3u8_index": m3u8_index,
        "output_path": str(output_path),
        "temp_dir": str(temp_dir),
        "max_segment_workers": max_segment_workers,
        "audio_tracks": [
            {"url": t["url"], "language": t.get("language", "und")}
            for t in (audio_track_urls or [])
            if t.get("url")
        ],
        "subtitle_tracks": [
            {"uri": t["uri"], "language": t.get("language", "und")}
            for t in (subtitle_track_urls or [])
            if t.get("uri")
        ],
        "subtitle_languages": list(subtitle_languages or []),
        "subtitle_mode": subtitle_mode,
    }
    if key:
        spec["key_hex"] = key
    if referer:
        spec["referer"] = referer
    if user_agent:
        spec["user_agent"] = user_agent
    if ffmpeg_path:
        # The panel resolves ffmpeg through app.core.ffmpeg_path; the engine must
        # use that same binary rather than whatever its own PATH turns up.
        spec["ffmpeg_path"] = ffmpeg_path

    bars = {}
    primary = []   # one-element list so the closure can assign to it
    # Last (segments, bytes) seen per phase. The engine reports running totals;
    # WebProgressBar wants increments, and keeps its own totals so it can
    # extrapolate the phase's eventual size and chain `prior_bytes` across
    # phases. Feeding it deltas means the panel's own readout stays the single
    # source of those numbers.
    seen = {}

    def bar_for(phase, total):
        """Reuse one progress bar per phase, matching the old per-phase bars."""
        if phase not in bars:
            if progress_factory is None:
                return None
            bars[phase] = progress_factory(total=total, phase=phase, unit="seg",
                                           desc="Downloading")
            if not primary:
                primary.append(bars[phase])
        return bars[phase]

    def on_event(event):
        kind = event.get("type")
        if kind == "progress":
            phase = event.get("phase")
            bar = bar_for(phase, event.get("total", 0))
            if bar is None:
                return
            previous_n, previous_bytes = seen.get(phase, (0, 0))
            n = event.get("current", 0)
            total_bytes = event.get("bytes", 0)
            seen[phase] = (n, total_bytes)
            # Never negative: a resume reports segments already on disk as one
            # jump, and a malformed frame must not rewind the bar.
            bar.update(max(0, n - previous_n), bytes=max(0, total_bytes - previous_bytes))
        elif kind == "status":
            # "joining" and "merging" belong to the download as a whole; the
            # frontend keys its phase list off whichever bar is live.
            bar = primary[0] if primary else bar_for(event.get("phase"), 0)
            if bar is not None and hasattr(bar, "emit_status"):
                bar.emit_status(event.get("phase"))

    return _EngineProcess(spec, cancel_event).run(on_event)
