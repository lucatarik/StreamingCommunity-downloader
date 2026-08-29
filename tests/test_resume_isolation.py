"""A job never has two attempts alive at once.

This is the guarantee the download engine was moved into a child process to
provide, and everything else about resume rests on it. Before, a forced resume
launched a second attempt immediately, because a blocked one could not be
stopped: the two then shared a job object and a temp directory, overwrote each
other's status, and — worse — each other's files. The workarounds for that (a
generation counter, per-generation temp folders, hardlink migration between
them, a "am I still current?" check before every write) were deleted once the
stop became enforceable, so these tests exist to keep that assumption honest.
"""

import threading
import time

import pytest

from app.jobs import ATTEMPT_STOP_TIMEOUT, JobManager
from app.progress import DownloadCancelledError


@pytest.fixture
def manager():
    return JobManager()


class Attempt:
    """A fake download that blocks until told to stop, and records overlap."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.live = 0
        self.max_live = 0
        self._lock = threading.Lock()
        self.runs = 0

    def __call__(self, *args, cancel_event=None, **kwargs):
        with self._lock:
            self.runs += 1
            self.live += 1
            self.max_live = max(self.max_live, self.live)
        self.started.set()
        try:
            # Stand in for the engine subprocess: stop when cancelled, as the
            # real bridge does once it has reaped the child.
            while not self.release.is_set():
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelledError("Annullato")
                time.sleep(0.01)
            return "/library/Film (2020)/Film (2020).mkv"
        finally:
            with self._lock:
                self.live -= 1


def _submit(manager: JobManager, attempt, monkeypatch) -> "object":
    """Start a film download whose actual work is `attempt`.

    Patched at `app.core.film.download_film` rather than passed in, because a
    resume rebuilds the call from the job's saved params — going through the
    real seam is what makes the test meaningful.
    """
    monkeypatch.setattr("app.core.film.download_film", attempt)
    job_id = manager.submit_film(1, "Film", "example.test", year="2020")
    job = manager.get(job_id)
    assert attempt.started.wait(timeout=5), "the first attempt never started"
    return job


def test_resume_waits_for_the_previous_attempt_to_stop(manager, monkeypatch):
    """The second attempt must not begin while the first is still running."""
    attempt = Attempt()
    job = _submit(manager, attempt, monkeypatch)
    job.stalled = True  # only a stalled running job may be force-resumed

    assert manager.resume(job.job_id) is True

    # Give the resume every chance to misbehave: if it launched immediately,
    # both attempts would be inside the fake download at once.
    time.sleep(0.3)
    assert attempt.max_live == 1, "two attempts were alive at the same time"

    # The first attempt observes its cancellation and exits, letting the second
    # start; then let that one finish.
    attempt.release.set()
    deadline = time.time() + 5
    while attempt.runs < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert attempt.runs == 2, "the resumed attempt never ran"
    assert attempt.max_live == 1


def test_resume_gets_a_fresh_cancel_event(manager, monkeypatch):
    """A resumed attempt must not inherit the flag that stopped the last one."""
    attempt = Attempt()
    job = _submit(manager, attempt, monkeypatch)
    job.stalled = True
    original = job.cancel_event

    manager.resume(job.job_id)

    assert job.cancel_event is not original
    assert original.is_set(), "the previous attempt must have been told to stop"
    assert not job.cancel_event.is_set()
    attempt.release.set()


def test_a_healthy_running_job_is_not_resumable(manager, monkeypatch):
    """Force-restarting a download that is making progress would be destructive."""
    attempt = Attempt()
    job = _submit(manager, attempt, monkeypatch)
    job.stalled = False

    assert manager.resume(job.job_id) is False
    assert attempt.runs == 1
    attempt.release.set()


def test_every_attempt_shares_one_temp_directory(manager):
    """Per-generation temp folders are gone; resume relies on reusing one.

    The engine writes each segment atomically, so a file visible in this
    directory is a finished file — which is what makes sharing it safe, and
    what makes a resume skip work instead of redoing it.
    """
    job = manager._make_job("Film", "film")
    job.params = {
        "id": 1, "title": "Film", "domain": "example.test", "year": None,
        "audio_languages": ["ita"], "subtitle_languages": [],
    }

    _, _, first = manager._build_call("film", job.params, job)
    _, _, second = manager._build_call("film", job.params, job)

    assert first["temp_dir"] == second["temp_dir"]
    assert "gen_" not in first["temp_dir"]
    assert job.job_id in first["temp_dir"]


def test_a_stuck_previous_attempt_blocks_the_resume_instead_of_racing_it(
    manager, monkeypatch
):
    """If the old attempt will not die, refuse rather than run two at once.

    Starting anyway is precisely the situation that corrupted files in
    production, so the job is failed with something the user can act on.
    """
    monkeypatch.setattr("app.jobs.ATTEMPT_STOP_TIMEOUT", 0.2)
    assert ATTEMPT_STOP_TIMEOUT > 0  # the real value is a sane default

    stuck = Attempt()

    def ignores_cancellation(*args, **kwargs):
        # Deliberately never checks cancel_event — the pathological case.
        stuck.started.set()
        stuck.release.wait(timeout=10)
        return "/library/never.mkv"

    monkeypatch.setattr("app.core.film.download_film", ignores_cancellation)
    job = manager.get(manager.submit_film(1, "Film", "example.test"))
    assert stuck.started.wait(timeout=5)
    job.stalled = True

    assert manager.resume(job.job_id) is True

    deadline = time.time() + 5
    while job.status != "error" and time.time() < deadline:
        time.sleep(0.01)

    assert job.status == "error", "a resume that cannot proceed must say so"
    assert "precedente" in (job.error or ""), job.error
    stuck.release.set()
