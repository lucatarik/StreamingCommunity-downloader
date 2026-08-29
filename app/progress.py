import asyncio
import time
from collections import deque
from typing import Callable, Optional


class DownloadCancelledError(Exception):
    pass


class WebProgressBar:
    """Drop-in tqdm replacement that pushes progress events onto an asyncio.Queue."""

    _EMIT_INTERVAL = 0.5  # seconds between progress events
    _SPEED_WINDOW = 5.0   # seconds for rolling speed average

    def __init__(
        self,
        total: int,
        job_queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        phase: str = None,
        on_event: Optional[Callable[[dict], None]] = None,
    ):
        self.total = total
        self.n = 0
        self._bytes = 0
        self._queue = job_queue
        self._loop = loop
        self._last_emit = 0.0
        self._phase = phase
        self._on_event = on_event
        self._start_time = time.monotonic()
        self._samples: deque = deque()       # (timestamp, n_segs) for ETA
        self._byte_samples: deque = deque()  # (timestamp, n_bytes) for display speed

    def _speed_and_eta(self) -> tuple[float, Optional[float], float]:
        now = time.monotonic()
        cutoff = now - self._SPEED_WINDOW

        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        if len(self._samples) < 2:
            elapsed = now - self._start_time
            seg_speed = self.n / elapsed if elapsed > 0 else 0.0
        else:
            dt = now - self._samples[0][0]
            seg_speed = (self.n - self._samples[0][1]) / dt if dt > 0 else 0.0

        while self._byte_samples and self._byte_samples[0][0] < cutoff:
            self._byte_samples.popleft()
        if len(self._byte_samples) < 2:
            elapsed = now - self._start_time
            bytes_speed = self._bytes / elapsed if elapsed > 0 else 0.0
        else:
            dt = now - self._byte_samples[0][0]
            bytes_speed = (self._bytes - self._byte_samples[0][1]) / dt if dt > 0 else 0.0

        remaining = self.total - self.n
        eta = (remaining / seg_speed) if seg_speed > 0 and remaining > 0 else None
        return round(seg_speed, 1), (round(eta) if eta is not None else None), round(bytes_speed)

    def _enqueue(self, msg: dict):
        """Hand a message to the SSE queue, if there is a loop to hand it to.

        The loop is only bound once the app has started, so a bar that emits
        before that (or in a test) must not leave an un-awaited coroutine
        behind — `JobManager._emit` already guards the same way.
        """
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._queue.put(msg), self._loop)

    def _push(self):
        seg_speed, eta, bytes_speed = self._speed_and_eta()
        pct = round(self.n / self.total * 100, 1) if self.total else 0
        msg = {
            "type": "progress",
            "current": self.n,
            "total": self.total,
            "pct": pct,
            "speed": seg_speed,
            "bytes_speed": bytes_speed,
            "eta": eta,
        }
        if self._phase:
            msg["phase"] = self._phase
        self._enqueue(msg)
        if self._on_event:
            self._on_event(msg)

    def update(self, n=1, bytes=0):
        self.n += n
        self._bytes += bytes
        now = time.monotonic()
        self._samples.append((now, self.n))
        self._byte_samples.append((now, self._bytes))
        if self.n >= self.total or now - self._last_emit >= self._EMIT_INTERVAL:
            self._last_emit = now
            self._push()

    def push_external(self, event: dict):
        """Forward a progress event computed by the download engine verbatim.

        The engine already tracks counts, speed and ETA over the same rolling
        window this class uses, so recomputing them here would only introduce a
        second, slightly different answer. `n` and `total` are kept in step so
        anything still reading them off the bar sees the truth.
        """
        msg = dict(event)
        msg["type"] = "progress"
        if self._phase and not msg.get("phase"):
            msg["phase"] = self._phase
        self.n = msg.get("current", self.n)
        self.total = msg.get("total", self.total)
        self._enqueue(msg)
        if self._on_event:
            self._on_event(msg)

    def emit_status(self, phase: str):
        msg = {"type": "status", "phase": phase}
        self._enqueue(msg)
        if self._on_event:
            self._on_event(msg)

    def close(self):
        pass

    def refresh(self):
        pass
