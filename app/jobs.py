import asyncio
import logging
import os
import shutil
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.config import VIDEOS_DIR, TMP_DIR, get_settings
from app.progress import DownloadCancelledError, WebProgressBar


def _get_library_path(type_: str) -> str:
    """Return the configured library path for content type, or VIDEOS_DIR as fallback."""
    from app.routers.domain import _read_data
    for lib in _read_data().get("libraries", []):
        if lib.get("type") == type_:
            return lib["path"]
    return str(VIDEOS_DIR)

logger = logging.getLogger(__name__)

SCHEDULER_INTERVAL = 30  # seconds

# How long a "running" job can go without a progress update before it is
# surfaced to the user as stalled. A CDN throttled to a trickle keeps a socket
# technically alive, so nothing else would ever raise.
STALL_THRESHOLD = 90  # seconds

# Phases that legitimately report no progress for minutes: ffmpeg is working
# through the whole film and there is nothing to count. Applying the stall
# heuristic to them painted healthy jobs as stuck and offered a "Forza ripresa"
# that would have discarded finished work.
NON_PROGRESS_PHASES = frozenset({"joining", "merging"})

# How long a forced resume waits for the previous attempt to actually stop.
# The engine is a child process that is SIGTERMed and then SIGKILLed, so this
# only has to cover the kill escalation plus the ffmpeg teardown.
ATTEMPT_STOP_TIMEOUT = 60  # seconds


@dataclass
class DownloadJob:
    job_id: str
    title: str
    type: str  # "film" | "episode" | "anime"
    status: str  # "scheduled" | "queued" | "running" | "done" | "error" | "cancelled"
    created_at: datetime
    scheduled_at: Optional[datetime] = None
    schedule_id: Optional[str] = None
    error: Optional[str] = None
    output_path: Optional[str] = None
    progress: dict = field(default_factory=lambda: {"current": 0, "total": 0, "pct": 0, "speed": 0, "eta": None})
    phases: list = field(default_factory=list)
    progress_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    cancel_event: threading.Event = field(default_factory=threading.Event)

    last_progress_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    stalled: bool = False
    # The phase the engine last announced, read by the stall check.
    phase: Optional[str] = None
    # The in-flight attempt. A forced resume waits on this before starting a new
    # one, which is what guarantees a job never has two live attempts.
    future: Optional[Future] = None
    # The call that runs this job, kept so a forced resume can repeat it.
    # Storing the call rather than the parameters keeps this independent of the
    # submit_*() signatures, which upstream changes freely.
    call: Optional[tuple] = None

    # Set when this job is one episode of a season or series asked for in one go,
    # so the notification can be a single summary instead of one per episode.
    batch_id: Optional[str] = None
    batch_kind: Optional[str] = None    # "season" | "series" | "anime_all"
    batch_label: Optional[str] = None   # computed once at submit, e.g. "Serie — Stagione 2"

    # Who asked for it. None in open mode, where there is no account to notify.
    user_id: Optional[int] = None

    # Kept as separate fields rather than parsed back out of `title`, which is
    # already a composed string ("Nome Serie S02E05") and would misparse the
    # first time a title itself contains something like S01.
    media_label: Optional[str] = None
    year: Optional[str] = None
    season: Optional[int] = None
    episode_number: Optional[str] = None


class JobManager:
    def __init__(self):
        self._jobs: dict[str, DownloadJob] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=64)
        n = get_settings().get("max_concurrent_downloads", 3)
        self._semaphore = threading.BoundedSemaphore(n)
        self._semaphore_value = n
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subscribers: list[asyncio.Queue] = []
        self._schedule_store = None  # set via set_schedule_store()
        self._listeners: list = []

    @staticmethod
    def _compute_phases(audio_languages: list) -> list:
        """Return the ordered list of phase names that will be emitted for this job."""
        steps = ["video", "joining"] + [f"audio_{l}" for l in (audio_languages or ["ita"])] + ["merging", "done"]
        return steps

    def update_max_concurrent(self, n: int):
        old = self._semaphore_value
        if n == old:
            return
        # Replace semaphore — existing running downloads are unaffected
        self._semaphore = threading.BoundedSemaphore(n)
        self._semaphore_value = n

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        loop.create_task(self._scheduler_loop())

    def set_schedule_store(self, store):
        from app.schedule import ScheduleStore
        self._schedule_store: Optional[ScheduleStore] = store

    def get(self, job_id: str) -> Optional[DownloadJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> list[dict]:
        with self._lock:
            return [self._job_to_dict(j) for j in self._jobs.values()]

    def _job_to_dict(self, job: DownloadJob) -> dict:
        return {
            "job_id": job.job_id,
            "title": job.title,
            "type": job.type,
            "status": job.status,
            "created_at": job.created_at.isoformat(),
            "scheduled_at": job.scheduled_at.isoformat() if job.scheduled_at else None,
            "schedule_id": job.schedule_id,
            "error": job.error,
            "output_path": job.output_path,
            "progress": job.progress,
            "phases": job.phases,
            "stalled": job.stalled,
            # user_id is deliberately absent: the jobs list is readable by anyone
            # who can download, and the field has no use in the interface.
            "batch_id": job.batch_id,
            "batch_kind": job.batch_kind,
            "season": job.season,
            "episode_number": job.episode_number,
            "year": job.year,
        }

    # ── Global pub/sub ─────────────────────────────────────────────────────────

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.append(q)
        return q

    def is_subscribed(self, q: asyncio.Queue) -> bool:
        """Whether this queue is still receiving events.

        The SSE endpoint checks this so a stream that has somehow been dropped
        ends the response instead of hanging: an EventSource only reconnects on
        a closed connection, never on a silent one.
        """
        return q in self._subscribers

    def unsubscribe(self, q: asyncio.Queue):
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def _broadcast(self, event: dict):
        """Thread-safe push to all global SSE subscribers."""
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._fanout(event), self._loop)

    def broadcast(self, event: dict):
        """Public entry point for other modules pushing to the SSE stream."""
        self._broadcast(event)

    # ── Terminal-state listeners ───────────────────────────────────────────────

    def add_listener(self, callback):
        """Register a callback invoked when a job reaches a terminal state.

        Used by the request system to move a request to completed or failed
        without the job manager knowing that requests exist.
        """
        self._listeners.append(callback)

    def _notify_listeners(self, job: "DownloadJob"):
        for callback in list(self._listeners):
            try:
                callback(job)
            except Exception:
                logger.exception("Job listener failed for job %s", job.job_id)

    def _emit(self, job: "DownloadJob", event: dict):
        """Push a terminal/progress event to the job's own queue and to everyone.

        The loop is only set once the app has started; a job finishing before
        that (or in a test) must not leave an un-awaited coroutine behind.
        """
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(job.progress_queue.put(event), self._loop)
        else:
            logger.debug("No event loop bound; dropping %s for job %s",
                         event.get("type"), job.job_id)
        self._broadcast({**event, "job_id": job.job_id})

    async def _fanout(self, event: dict):
        """Push an event to every subscriber, dropping the oldest under pressure.

        A backgrounded tab, a slow proxy, or a season starting twenty downloads
        at once can all fill a queue. Dropping the *subscriber* there — which is
        what used to happen — left the browser holding an open connection nobody
        would ever write to again: the page looked live and silently stopped
        updating until it was reloaded. Discarding the oldest event instead
        costs a stale frame of progress and lets the next one repair the view.
        """
        for q in list(self._subscribers):
            while True:
                try:
                    q.put_nowait(event)
                    break
                except asyncio.QueueFull:
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        # Drained from under us by its own reader; try again.
                        continue

    # ── Progress factory ───────────────────────────────────────────────────────

    def _make_progress_factory(self, job: DownloadJob):
        loop = self._loop
        manager = self

        def on_event(ev: dict):
            def alive(phase):
                job.phase = phase or job.phase
                job.last_progress_at = datetime.now(timezone.utc)
                if job.stalled:
                    job.stalled = False
                    manager._broadcast({"type": "job_unstalled", "job_id": job.job_id})

            if ev.get("type") == "status":
                # A phase transition proves the engine is alive even when it has
                # nothing to count, so it resets the stall clock too.
                alive(ev.get("phase"))
            if ev.get("type") == "progress":
                alive(ev.get("phase"))
                job.progress = {
                    "current": ev["current"],
                    "total": ev["total"],
                    "pct": ev["pct"],
                    "speed": ev.get("speed", 0),
                    "bytes_speed": ev.get("bytes_speed", 0),
                    "eta": ev.get("eta"),
                }
            manager._broadcast({**ev, "job_id": job.job_id})

        def factory(**kwargs):
            total = kwargs.get("total", 0)
            phase = kwargs.get("phase")
            return WebProgressBar(total, job.progress_queue, loop, phase=phase, on_event=on_event)

        return factory

    # ── Scheduler loop ─────────────────────────────────────────────────────────

    async def _scheduler_loop(self):
        while True:
            await asyncio.sleep(SCHEDULER_INTERVAL)
            now = datetime.now(timezone.utc)
            with self._lock:
                jobs = list(self._jobs.values())

            self._check_stalls(jobs, now)

            for job in jobs:
                if job.status != "scheduled" or job.scheduled_at is None or job.schedule_id is None:
                    continue
                sa = job.scheduled_at
                if sa.tzinfo is None:
                    sa = sa.replace(tzinfo=timezone.utc)
                if sa > now:
                    continue
                if self._schedule_store is None:
                    continue
                entry = self._schedule_store.get_by_schedule_id(job.schedule_id)
                if entry is None:
                    continue
                logger.info("Scheduler firing job_id=%s schedule_id=%s", job.job_id, job.schedule_id)
                try:
                    self._fire_job(job, entry["type"], entry["params"])
                except Exception as e:
                    logger.error("Failed to fire scheduled job %s: %s", job.job_id, e)

    def _check_stalls(self, jobs: list, now: datetime):
        """Flag running jobs that have gone quiet for too long.

        Phases that report no progress by design are excluded — see
        NON_PROGRESS_PHASES.
        """
        for job in jobs:
            if job.status != "running" or job.stalled:
                continue
            if job.phase in NON_PROGRESS_PHASES:
                continue
            if (now - job.last_progress_at).total_seconds() > STALL_THRESHOLD:
                job.stalled = True
                logger.warning("Job %s appears stalled (no progress for >%ss)",
                               job.job_id, STALL_THRESHOLD)
                self._broadcast({"type": "job_stalled", "job_id": job.job_id})

    def _fire_job(self, job: DownloadJob, type_: str, params: dict):
        fn, args, kwargs = self._build_call(type_, params, job)
        with self._lock:
            job.status = "queued"
            if self._schedule_store is not None and job.schedule_id:
                self._schedule_store.mark_fired(job.schedule_id)
        self._broadcast({"type": "job_status", "job_id": job.job_id, "status": "queued"})
        job.call = (fn, args, kwargs)
        self._track(job, self._executor.submit(self._run_download, job, fn, *args, **kwargs))

    def _build_call(self, type_: str, params: dict, job: DownloadJob):
        pf = self._make_progress_factory(job)
        td = str(TMP_DIR / job.job_id)
        if type_ == "film":
            from app.core.film import download_film
            return download_film, (params["id"], params["title"], params["domain"]), dict(
                output_dir=_get_library_path("film"), temp_dir=td, progress_factory=pf,
                year=params.get("year"), cancel_event=job.cancel_event,
                audio_languages=params.get("audio_languages", ["ita"]),
                subtitle_languages=params.get("subtitle_languages", ["ita", "eng"]),
                tmdb_id=params.get("tmdb_id"),
            )
        if type_ == "episode":
            from app.core.tv import download_episode
            return download_episode, (
                params["tv_id"], params["eps"], params["ep_index"],
                params["domain"], params["token"], params["tv_name"], params["season"],
            ), dict(
                output_dir=_get_library_path("tv"), temp_dir=td, progress_factory=pf,
                cancel_event=job.cancel_event, year=params.get("year"),
                audio_languages=params.get("audio_languages", ["ita"]),
                subtitle_languages=params.get("subtitle_languages", ["ita", "eng"]),
                tmdb_id=params.get("tmdb_id"),
            )
        if type_ == "anime":
            from app.core.animeunity import download_anime_episode
            return download_anime_episode, (
                params["anime_id"], params["episode"],
                params["anime_name"], params.get("anime_type", "tv"),
            ), dict(
                output_dir=_get_library_path("anime"), temp_dir=td, progress_factory=pf,
                cancel_event=job.cancel_event, year=params.get("year"),
                audio_languages=params.get("audio_languages", ["ita"]),
                subtitle_languages=params.get("subtitle_languages", ["ita", "eng"]),
            )
        raise ValueError(f"Unknown schedule type: {type_!r}")

    # ── Job lifecycle ──────────────────────────────────────────────────────────

    def fire_now(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status != "scheduled" or job.schedule_id is None:
            return False
        entry = self._schedule_store.get_by_schedule_id(job.schedule_id) if self._schedule_store else None
        if entry is None:
            return False
        self._fire_job(job, entry["type"], entry["params"])
        return True

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status not in ("scheduled", "queued", "running"):
                return False
            # A scheduled job was never handed to the executor, so _run_download
            # will never run for it and nothing else would ever tell listeners it
            # is over. Queued and running ones go through _run_download, which
            # notifies on every path.
            was_scheduled = job.status == "scheduled"
            job.cancel_event.set()
            job.status = "cancelled"
            job.stalled = False
        self._emit(job, {"type": "error", "message": "Annullato"})
        if was_scheduled:
            self._notify_listeners(job)
        return True

    def resume(self, job_id: str) -> bool:
        """Force-resume a stalled, failed or cancelled job.

        The previous attempt is cancelled and then *waited for* before a new one
        starts, on a worker thread so the caller's request returns immediately.
        That wait is the design: the download runs in a child process that can
        actually be killed, so "cancelled" means stopped, and a job can never
        have two attempts alive at once writing the same files.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            if job.status == "running" and not job.stalled:
                return False  # a healthy download is not ours to restart
            if job.status not in ("running", "error", "cancelled"):
                return False
            if job.call is None:
                return False  # a scheduled job not yet fired has nothing to repeat
            previous = job.future
            job.cancel_event.set()
            job.cancel_event = threading.Event()
            fn, args, kwargs = job.call
            # The stored call carries the cancel_event of the attempt that just
            # died; the new one needs the fresh event or it would start already
            # cancelled.
            if "cancel_event" in kwargs:
                kwargs = {**kwargs, "cancel_event": job.cancel_event}
                job.call = (fn, args, kwargs)
            job.status = "queued"
            job.stalled = False
            job.error = None
            job.phase = None
            job.last_progress_at = datetime.now(timezone.utc)

        self._broadcast({"type": "job_status", "job_id": job.job_id,
                         "status": "queued", "stalled": False})
        self._track(job, self._executor.submit(
            self._stop_then_run, job, previous, fn, *args, **kwargs
        ))
        return True

    def _track(self, job: DownloadJob, future: Future):
        with self._lock:
            job.future = future

    def _stop_then_run(self, job: DownloadJob, previous: Optional[Future], fn, *args, **kwargs):
        """Wait for the previous attempt to finish, then run a new one."""
        if previous is not None and not previous.done():
            logger.info("Job %s: attendo la fine del tentativo precedente", job.job_id)
            try:
                previous.result(timeout=ATTEMPT_STOP_TIMEOUT)
            except FutureTimeout:
                # Refusing is the safe answer: a second attempt would be exactly
                # the concurrent-writer situation this design exists to prevent.
                logger.error(
                    "Job %s: il tentativo precedente non si e' fermato entro %ss, resume annullato",
                    job.job_id, ATTEMPT_STOP_TIMEOUT,
                )
                job.status = "error"
                job.error = ("Il tentativo precedente non si e' fermato: "
                             "riprova fra qualche istante.")
                self._emit(job, {"type": "error", "message": job.error})
                self._notify_listeners(job)
                return
            except Exception:
                # It ended by raising. _run_download already reported that, and
                # it is now over, which is all this needs to know.
                logger.info("Job %s: il tentativo precedente e' terminato con errore", job.job_id)

        self._run_download(job, fn, *args, **kwargs)

    def dismiss(self, job_id: str) -> bool:
        """Remove a finished/cancelled job and clean it from the schedule store."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status not in ("done", "error", "cancelled"):
                return False
            del self._jobs[job_id]
        if self._schedule_store is not None:
            self._schedule_store.remove_by_job_id(job_id)
        # A failed job keeps its segments so a resume can use them; dismissing it
        # is the user saying they will not resume.
        tmp_path = TMP_DIR / job_id
        if tmp_path.exists():
            shutil.rmtree(tmp_path, ignore_errors=True)
            logger.info("Cleaned up temp dir for dismissed job: %s", tmp_path)
        self._broadcast({"type": "job_dismissed", "job_id": job_id})
        return True

    def _run_download(self, job: DownloadJob, fn, *args, **kwargs):
        # Listeners fire outside the semaphore: a listener does DB writes and
        # blocking HTTP (external notification channels), and holding a download
        # slot for that would cost real throughput. The outer try/finally makes
        # every path notify exactly once — including the job cancelled before it
        # ever started, which used to return early and notify nobody, leaving
        # anything counting completions waiting forever.
        try:
            with self._semaphore:
                if job.cancel_event.is_set():
                    job.status = "cancelled"
                    job.stalled = False
                    self._emit(job, {"type": "error", "message": "Annullato"})
                    return

                job.status = "running"
                self._broadcast({"type": "job_status", "job_id": job.job_id, "status": "running"})

                try:
                    result = fn(*args, **kwargs)
                    job.status = "done"
                    job.stalled = False
                    job.output_path = result
                    self._emit(job, {"type": "done", "output_path": result})
                except DownloadCancelledError:
                    job.status = "cancelled"
                    job.stalled = False
                    self._emit(job, {"type": "error", "message": "Annullato"})
                except Exception as e:
                    logger.exception(f"Job {job.job_id} failed: {e}")
                    job.status = "error"
                    job.stalled = False
                    job.error = str(e)
                    self._emit(job, {"type": "error", "message": str(e)})
                finally:
                    # Only a completed download clears its scratch space. A
                    # failed or cancelled one keeps it, which is what lets a
                    # forced resume pick up the segments already on disk — the
                    # engine writes them atomically, so what is there is whole.
                    if job.status == "done":
                        tmp_path = TMP_DIR / job.job_id
                        if tmp_path.exists():
                            shutil.rmtree(tmp_path, ignore_errors=True)
                            logger.info("Cleaned up temp dir: %s", tmp_path)
        finally:
            self._notify_listeners(job)

    def _submit_job(self, job: DownloadJob, fn, *args, **kwargs) -> str:
        with self._lock:
            self._jobs[job.job_id] = job
        self._broadcast({"type": "job_created", "job": self._job_to_dict(job)})
        job.call = (fn, args, kwargs)
        self._track(job, self._executor.submit(self._run_download, job, fn, *args, **kwargs))
        return job.job_id

    def _make_job(self, title: str, type_: str, scheduled_at: Optional[datetime] = None,
                  schedule_id: Optional[str] = None, phases: list = None,
                  **meta) -> DownloadJob:
        """Build a job. ``meta`` carries the notification fields declared on
        DownloadJob (batch_id, user_id, season, …); unknown keys raise, which is
        the point — a typo must not silently vanish."""
        now = datetime.now(timezone.utc)
        sa = scheduled_at.replace(tzinfo=timezone.utc) if scheduled_at and scheduled_at.tzinfo is None else scheduled_at
        status = "scheduled" if sa and sa > now else "queued"
        return DownloadJob(
            job_id=str(uuid.uuid4()),
            title=title,
            type=type_,
            status=status,
            # Same instant `status` was decided against, and timezone-aware like
            # scheduled_at so the two stay comparable.
            created_at=now,
            scheduled_at=scheduled_at,
            schedule_id=schedule_id,
            phases=phases or [],
            **meta,
        )

    # ── Submit (immediate) ─────────────────────────────────────────────────────

    def submit_film(self, id_film: int, title: str, domain: str, year: str = None,
                    schedule_id: str = None,
                    audio_languages: list[str] = None,
                    subtitle_languages: list[str] = None,
                    strict_audio: bool = False,
                    user_id: int = None,
                    tmdb_id: int = None) -> str:
        from app.core.film import download_film

        job = self._make_job(title, "film", schedule_id=schedule_id,
                             phases=self._compute_phases(audio_languages or ["ita"]),
                             user_id=user_id, media_label=title, year=year)
        return self._submit_job(
            job, download_film,
            id_film, title, domain,
            output_dir=_get_library_path("film"),
            temp_dir=str(TMP_DIR / job.job_id),
            progress_factory=self._make_progress_factory(job),
            year=year,
            cancel_event=job.cancel_event,
            audio_languages=audio_languages or ["ita"],
            subtitle_languages=subtitle_languages or ["ita", "eng"],
            strict_audio=strict_audio,
            tmdb_id=tmdb_id,
        )

    def submit_episode(self, tv_id: int, eps: list[dict], ep_index: int, domain: str,
                       token: str, tv_name: str, season: int, year: str = None,
                       schedule_id: str = None,
                       audio_languages: list[str] = None,
                       subtitle_languages: list[str] = None,
                       strict_audio: bool = False,
                       user_id: int = None, batch_id: str = None,
                       batch_kind: str = None, batch_label: str = None,
                       tmdb_id: int = None) -> str:
        from app.core.tv import download_episode, fmt_ep

        ep = eps[ep_index]
        title = f"{tv_name} S{season:02d}E{fmt_ep(ep['n'])}"
        job = self._make_job(title, "episode", schedule_id=schedule_id,
                             phases=self._compute_phases(audio_languages or ["ita"]),
                             user_id=user_id, batch_id=batch_id, batch_kind=batch_kind,
                             batch_label=batch_label, media_label=tv_name, year=year,
                             season=season, episode_number=str(ep["n"]))
        return self._submit_job(
            job, download_episode,
            tv_id, eps, ep_index, domain, token, tv_name, season,
            output_dir=_get_library_path("tv"),
            temp_dir=str(TMP_DIR / job.job_id),
            progress_factory=self._make_progress_factory(job),
            cancel_event=job.cancel_event,
            year=year,
            audio_languages=audio_languages or ["ita"],
            subtitle_languages=subtitle_languages or ["ita", "eng"],
            strict_audio=strict_audio,
            tmdb_id=tmdb_id,
        )

    # AnimeUnity deliberately gains no tmdb_id: an anime here has no TMDB
    # identifier and its embed lives on a different host, so a fallback
    # provider would have nothing to resolve against.
    def submit_anime_episode(self, anime_id: str, episode: dict, anime_name: str,
                             anime_type: str = "tv", year: str = None,
                             schedule_id: str = None,
                             audio_languages: list[str] = None,
                             subtitle_languages: list[str] = None,
                             strict_audio: bool = False,
                             user_id: int = None, batch_id: str = None,
                             batch_kind: str = None, batch_label: str = None) -> str:
        from app.core.animeunity import download_anime_episode

        ep_num = episode.get("number", "?")
        title = f"{anime_name} E{ep_num}"
        job = self._make_job(title, "anime", schedule_id=schedule_id,
                             phases=self._compute_phases(audio_languages or ["ita"]),
                             user_id=user_id, batch_id=batch_id, batch_kind=batch_kind,
                             batch_label=batch_label, media_label=anime_name, year=year,
                             episode_number=str(ep_num))
        return self._submit_job(
            job, download_anime_episode,
            anime_id, episode, anime_name, anime_type,
            output_dir=_get_library_path("anime"),
            temp_dir=str(TMP_DIR / job.job_id),
            progress_factory=self._make_progress_factory(job),
            cancel_event=job.cancel_event,
            year=year,
            audio_languages=audio_languages or ["ita"],
            subtitle_languages=subtitle_languages or ["ita", "eng"],
            strict_audio=strict_audio,
        )

    # ── Schedule (future) ──────────────────────────────────────────────────────

    def schedule_film(self, id_film: int, title: str, domain: str,
                      scheduled_at: datetime, year: str = None,
                      audio_languages: list[str] = None,
                      subtitle_languages: list[str] = None,
                      user_id: int = None,
                      tmdb_id: int = None) -> str:
        params = {
            "id": id_film, "title": title, "domain": domain, "year": year,
            "audio_languages": audio_languages or ["ita"],
            "subtitle_languages": subtitle_languages or ["ita", "eng"],
            # Persisted with the schedule: the cache it came from will not
            # survive until the job fires.
            "tmdb_id": tmdb_id,
        }
        return self._add_schedule("film", scheduled_at, params, title,
                                  user_id=user_id, media_label=title, year=year)

    def schedule_episode(self, tv_id: int, eps: list[dict], ep_index: int, domain: str,
                         token: str, tv_name: str, season: int,
                         scheduled_at: datetime, year: str = None,
                         audio_languages: list[str] = None,
                         subtitle_languages: list[str] = None,
                         user_id: int = None, batch_id: str = None,
                         batch_kind: str = None, batch_label: str = None,
                         tmdb_id: int = None) -> str:
        from app.core.tv import fmt_ep
        ep = eps[ep_index]
        title = f"{tv_name} S{season:02d}E{fmt_ep(ep['n'])}"
        params = {
            "tv_id": tv_id, "eps": eps, "ep_index": ep_index,
            "domain": domain, "token": token, "tv_name": tv_name,
            "season": season, "year": year,
            "audio_languages": audio_languages or ["ita"],
            "subtitle_languages": subtitle_languages or ["ita", "eng"],
            "tmdb_id": tmdb_id,
        }
        return self._add_schedule("episode", scheduled_at, params, title,
                                  user_id=user_id, batch_id=batch_id, batch_kind=batch_kind,
                                  batch_label=batch_label, media_label=tv_name, year=year,
                                  season=season, episode_number=str(ep["n"]))

    def schedule_anime_episode(self, anime_id: str, episode: dict, anime_name: str,
                               scheduled_at: datetime, anime_type: str = "tv",
                               year: str = None,
                               audio_languages: list[str] = None,
                               subtitle_languages: list[str] = None,
                               user_id: int = None, batch_id: str = None,
                               batch_kind: str = None, batch_label: str = None) -> str:
        ep_num = episode.get("number", "?")
        title = f"{anime_name} E{ep_num}"
        params = {
            "anime_id": anime_id, "episode": episode, "anime_name": anime_name,
            "anime_type": anime_type, "year": year,
            "audio_languages": audio_languages or ["ita"],
            "subtitle_languages": subtitle_languages or ["ita", "eng"],
        }
        return self._add_schedule("anime", scheduled_at, params, title,
                                  user_id=user_id, batch_id=batch_id, batch_kind=batch_kind,
                                  batch_label=batch_label, media_label=anime_name, year=year,
                                  episode_number=str(ep_num))

    def _add_schedule(self, type_: str, scheduled_at: datetime, params: dict, title: str,
                      **meta) -> str:
        if self._schedule_store is None:
            raise RuntimeError("ScheduleStore not configured")
        schedule_id = self._schedule_store.add(type_, scheduled_at, params)
        # Firing reuses this same job object, so notification metadata set here
        # survives until the download actually runs. It does not survive a
        # restart (load_scheduled_from_store rebuilds from the JSON params), and
        # neither does the in-memory batch it would belong to.
        job = self._make_job(title, type_, scheduled_at=scheduled_at, schedule_id=schedule_id,
                             phases=self._compute_phases(params.get("audio_languages") or ["ita"]),
                             **meta)
        with self._lock:
            self._jobs[job.job_id] = job
        self._schedule_store.set_job_id(schedule_id, job.job_id)
        self._broadcast({"type": "job_created", "job": self._job_to_dict(job)})
        return job.job_id

    def load_scheduled_from_store(self):
        """Re-hydrate pending scheduled entries from the JSON store on startup."""
        if self._schedule_store is None:
            return
        for entry in self._schedule_store.list_all():
            if entry.get("fired"):
                continue  # already dispatched in a previous session — skip
            sid = entry["schedule_id"]
            type_ = entry["type"]
            params = entry["params"]
            sa_raw = datetime.fromisoformat(entry["scheduled_at"])
            scheduled_at = sa_raw if sa_raw.tzinfo else sa_raw.replace(tzinfo=timezone.utc)
            title = params.get("title") or params.get("tv_name") or params.get("anime_name", "?")
            job = self._make_job(title, type_, scheduled_at=scheduled_at, schedule_id=sid,
                                 phases=self._compute_phases(params.get("audio_languages") or ["ita"]))
            with self._lock:
                self._jobs[job.job_id] = job
            self._schedule_store.set_job_id(sid, job.job_id)
            logger.info("Restored scheduled job %s (schedule_id=%s) for %s", job.job_id, sid, scheduled_at)


job_manager = JobManager()
