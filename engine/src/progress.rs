//! Progress accounting and the stdout event channel.
//!
//! Emission goes through an unbounded channel drained by one writer task, so a
//! segment worker never blocks on stdout — and so events can never interleave
//! mid-line, which a shared `println!` across 16 workers would eventually do.

use std::collections::VecDeque;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tokio::sync::mpsc::UnboundedSender;

use crate::spec::Event;

/// Matches `WebProgressBar._EMIT_INTERVAL` — the panel's SSE stream and its
/// frontend were tuned around this cadence.
const EMIT_INTERVAL: Duration = Duration::from_millis(500);
/// Matches `WebProgressBar._SPEED_WINDOW`.
const SPEED_WINDOW: Duration = Duration::from_secs(5);

#[derive(Clone)]
pub struct EventSink {
    tx: UnboundedSender<Event>,
}

impl EventSink {
    pub fn new(tx: UnboundedSender<Event>) -> Self {
        Self { tx }
    }

    pub fn emit(&self, event: Event) {
        // A closed receiver means the writer task is gone (the panel hung up).
        // Losing a progress line then is correct and uninteresting.
        let _ = self.tx.send(event);
    }

    pub fn status(&self, phase: &str) {
        self.emit(Event::Status {
            phase: phase.to_string(),
        });
    }
}

struct State {
    current: u64,
    bytes: u64,
    started: Instant,
    last_emit: Option<Instant>,
    seg_samples: VecDeque<(Instant, u64)>,
    byte_samples: VecDeque<(Instant, u64)>,
}

pub struct Progress {
    phase: Option<String>,
    total: u64,
    sink: EventSink,
    state: Mutex<State>,
}

impl Progress {
    pub fn new(sink: EventSink, phase: Option<String>, total: u64) -> Self {
        Self {
            phase,
            total,
            sink,
            state: Mutex::new(State {
                current: 0,
                bytes: 0,
                started: Instant::now(),
                last_emit: None,
                seg_samples: VecDeque::new(),
                byte_samples: VecDeque::new(),
            }),
        }
    }

    /// Record `segments` finished units and `bytes` transferred.
    ///
    /// Only segments that are genuinely on disk are counted. The Python bar was
    /// advanced even for segments whose fetch had failed, which is how a
    /// download with 2929 missing segments still displayed 100%.
    pub fn advance(&self, segments: u64, bytes: u64) {
        let now = Instant::now();

        // A poisoned lock would mean a worker panicked mid-update. Progress
        // reporting must not be the thing that takes the download down, so the
        // count is recovered and carried on.
        let mut state = match self.state.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        };

        state.current += segments;
        state.bytes += bytes;
        let (current, total_bytes) = (state.current, state.bytes);
        state.seg_samples.push_back((now, current));
        state.byte_samples.push_back((now, total_bytes));

        let cutoff = now.checked_sub(SPEED_WINDOW);
        if let Some(cutoff) = cutoff {
            while state.seg_samples.front().is_some_and(|(t, _)| *t < cutoff) {
                state.seg_samples.pop_front();
            }
            while state.byte_samples.front().is_some_and(|(t, _)| *t < cutoff) {
                state.byte_samples.pop_front();
            }
        }

        let due = match state.last_emit {
            None => true,
            Some(last) => now.duration_since(last) >= EMIT_INTERVAL,
        };
        // The final update always goes out, so the bar never freezes one tick
        // short of the end.
        if !due && state.current < self.total {
            return;
        }
        state.last_emit = Some(now);

        let seg_speed = rate(&state.seg_samples, state.current, state.started, now);
        let byte_speed = rate(&state.byte_samples, state.bytes, state.started, now);
        let remaining = self.total.saturating_sub(state.current);
        let eta = if seg_speed > 0.0 && remaining > 0 {
            Some((remaining as f64 / seg_speed).round() as u64)
        } else {
            None
        };
        let pct = if self.total > 0 {
            (state.current as f64 / self.total as f64 * 1000.0).round() / 10.0
        } else {
            0.0
        };

        self.sink.emit(Event::Progress {
            phase: self.phase.clone(),
            current: state.current,
            total: self.total,
            pct,
            speed: (seg_speed * 10.0).round() / 10.0,
            bytes_speed: byte_speed.round() as u64,
            eta,
        });
    }
}

/// Units per second over the rolling window, falling back to the average since
/// start while the window holds fewer than two samples.
fn rate(samples: &VecDeque<(Instant, u64)>, current: u64, started: Instant, now: Instant) -> f64 {
    if samples.len() >= 2
        && let Some((oldest_at, oldest_value)) = samples.front() {
            let dt = now.duration_since(*oldest_at).as_secs_f64();
            if dt > 0.0 {
                return (current.saturating_sub(*oldest_value)) as f64 / dt;
            }
        }
    let elapsed = now.duration_since(started).as_secs_f64();
    if elapsed > 0.0 { current as f64 / elapsed } else { 0.0 }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::sync::mpsc;

    fn drain(rx: &mut mpsc::UnboundedReceiver<Event>) -> Vec<Event> {
        let mut out = Vec::new();
        while let Ok(event) = rx.try_recv() {
            out.push(event);
        }
        out
    }

    #[test]
    fn the_first_and_last_updates_always_emit() {
        let (tx, mut rx) = mpsc::unbounded_channel();
        let progress = Progress::new(EventSink::new(tx), Some("video".into()), 3);

        progress.advance(1, 100);
        progress.advance(1, 100); // throttled: inside the emit interval
        progress.advance(1, 100); // completion: emitted regardless

        let events = drain(&mut rx);
        assert_eq!(events.len(), 2, "expected first and final emission only");

        match &events[1] {
            Event::Progress {
                current,
                total,
                pct,
                phase,
                ..
            } => {
                assert_eq!(*current, 3);
                assert_eq!(*total, 3);
                assert_eq!(*pct, 100.0);
                assert_eq!(phase.as_deref(), Some("video"));
            }
            other => panic!("expected a progress event, got {other:?}"),
        }
    }

    #[test]
    fn a_zero_segment_job_does_not_divide_by_zero() {
        let (tx, mut rx) = mpsc::unbounded_channel();
        let progress = Progress::new(EventSink::new(tx), None, 0);
        progress.advance(0, 0);
        match drain(&mut rx).first() {
            Some(Event::Progress { pct, eta, .. }) => {
                assert_eq!(*pct, 0.0);
                assert!(eta.is_none());
            }
            other => panic!("expected a progress event, got {other:?}"),
        }
    }

    #[test]
    fn rate_falls_back_to_the_average_before_the_window_fills() {
        let now = Instant::now();
        let started = now - Duration::from_secs(4);
        let samples = VecDeque::from(vec![(now, 8u64)]);
        assert!((rate(&samples, 8, started, now) - 2.0).abs() < 0.01);
    }
}
