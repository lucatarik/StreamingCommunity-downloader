"""A slow SSE consumer must lose events, never its connection.

The downloads tab used to stop updating until the page was reloaded. The cause
was here: a subscriber whose queue filled up was unsubscribed, but its SSE
generator stayed alive, blocked on a queue nobody would ever fill again, and
kept emitting keepalives. The browser therefore saw a healthy connection,
EventSource never fired `onerror`, and no reconnect — and no fresh snapshot —
ever happened. A backgrounded tab consuming slowly was enough to trigger it.
"""

import asyncio

import pytest

from app.jobs import JobManager


@pytest.fixture
def manager():
    return JobManager()


def _fill(q: asyncio.Queue, count: int):
    for i in range(count):
        q.put_nowait({"type": "progress", "seq": i})


def test_a_saturated_consumer_keeps_its_subscription(manager):
    q = manager.subscribe()
    _fill(q, q.maxsize)
    assert q.full()

    asyncio.run(manager._fanout({"type": "job_created", "job_id": "abc"}))

    assert manager.is_subscribed(q), (
        "dropping the subscriber leaves its SSE stream alive but deaf, which is "
        "invisible to the browser"
    )


def test_the_oldest_event_is_dropped_not_the_newest(manager):
    """The newest event matters most; the oldest is a superseded progress tick."""
    q = manager.subscribe()
    _fill(q, q.maxsize)

    asyncio.run(manager._fanout({"type": "job_created", "job_id": "abc"}))

    drained = []
    while not q.empty():
        drained.append(q.get_nowait())

    assert drained[-1] == {"type": "job_created", "job_id": "abc"}
    assert drained[0]["seq"] == 1, "the oldest queued event should have gone"
    assert len(drained) == q.maxsize


def test_a_healthy_consumer_receives_everything(manager):
    q = manager.subscribe()
    events = [{"type": "job_status", "n": i} for i in range(5)]
    for event in events:
        asyncio.run(manager._fanout(event))

    drained = [q.get_nowait() for _ in range(q.qsize())]
    assert drained == events


def test_unsubscribing_is_observable(manager):
    """The SSE endpoint ends the response on this, so it has to be truthful."""
    q = manager.subscribe()
    assert manager.is_subscribed(q) is True

    manager.unsubscribe(q)
    assert manager.is_subscribed(q) is False

    # And a fanout to nobody must not raise.
    asyncio.run(manager._fanout({"type": "job_created"}))


def test_fanout_reaches_every_subscriber(manager):
    first, second = manager.subscribe(), manager.subscribe()
    asyncio.run(manager._fanout({"type": "job_dismissed", "job_id": "x"}))

    assert first.get_nowait() == {"type": "job_dismissed", "job_id": "x"}
    assert second.get_nowait() == {"type": "job_dismissed", "job_id": "x"}


def test_one_saturated_consumer_does_not_starve_another(manager):
    slow, fast = manager.subscribe(), manager.subscribe()
    _fill(slow, slow.maxsize)

    asyncio.run(manager._fanout({"type": "done", "job_id": "x"}))

    assert manager.is_subscribed(slow)
    assert fast.get_nowait() == {"type": "done", "job_id": "x"}
