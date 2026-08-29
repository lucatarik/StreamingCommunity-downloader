"""Stall detection must not fire on phases that are silent by design.

ffmpeg muxing a two-hour film emits no progress for minutes. Flagging that as
stalled painted a healthy job orange and offered a "Forza ripresa" button that
would have thrown away a finished download to start it over.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.jobs import NON_PROGRESS_PHASES, STALL_THRESHOLD, JobManager


@pytest.fixture
def manager():
    return JobManager()


def _running_job(manager: JobManager, phase, silent_for: float):
    job = manager._make_job("Film", "film")
    job.status = "running"
    job.phase = phase
    job.last_progress_at = datetime.now(timezone.utc) - timedelta(seconds=silent_for)
    return job


def _check(manager: JobManager, job):
    manager._check_stalls([job], datetime.now(timezone.utc))
    return job.stalled


@pytest.mark.parametrize("phase", sorted(NON_PROGRESS_PHASES))
def test_muxing_phases_are_never_flagged_as_stalled(manager, phase):
    job = _running_job(manager, phase, silent_for=STALL_THRESHOLD * 10)
    assert _check(manager, job) is False, f"{phase} is silent by design"


def test_a_silent_download_phase_is_still_flagged(manager):
    """The heuristic must keep working where it is actually meaningful."""
    job = _running_job(manager, "video", silent_for=STALL_THRESHOLD + 10)
    assert _check(manager, job) is True


@pytest.mark.parametrize("phase", ["video", "audio_ita"])
def test_a_progressing_job_is_left_alone(manager, phase):
    job = _running_job(manager, phase, silent_for=1)
    assert _check(manager, job) is False


def test_only_running_jobs_are_considered(manager):
    """A finished job that sat idle must not turn orange after the fact."""
    job = _running_job(manager, "video", silent_for=STALL_THRESHOLD * 5)
    job.status = "done"
    assert _check(manager, job) is False


def test_a_phase_transition_clears_a_stall_and_resets_the_clock(manager):
    """Announcing a new phase is proof of life, even with nothing to count."""
    job = _running_job(manager, "video", silent_for=STALL_THRESHOLD + 10)
    assert _check(manager, job) is True

    factory = manager._make_progress_factory(job)
    bar = factory(total=0, phase="joining")
    bar.emit_status("joining")

    assert job.stalled is False
    assert job.phase == "joining"
    # And it stays clear now that it is in a silent phase.
    assert _check(manager, job) is False
