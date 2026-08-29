//! The retrying HTTP client.
//!
//! Two of the three bugs found in production lived here, in the Python version:
//!
//! * `get_info()` fetched the rendition playlist exactly once, so one transient
//!   CDN 500 killed the whole download.
//! * `get_req_ts()` looked like it retried three times, but only 429 actually
//!   looped — every other status returned on the first attempt. A burst of
//!   503s silently dropped 2929 segments from one real download while the
//!   progress bar still read 100%.
//!
//! Both were the same mistake: deciding *per status code* whether to retry, in
//! code duplicated at each call site. Here the retry policy is written once,
//! and the classification is exhaustive — a status is transient, permanent, or
//! success, with no fourth path that quietly gives up.

use std::time::Duration;

use reqwest::StatusCode;
use tokio_util::sync::CancellationToken;

use crate::error::{EngineError, Result};

/// Attempts per request, including the first.
const MAX_ATTEMPTS: u32 = 5;
const BASE_BACKOFF_MS: u64 = 500;
const MAX_BACKOFF: Duration = Duration::from_secs(20);

/// How long a single body read may produce nothing before the attempt is
/// abandoned. This is the fix for the stall the panel could only paper over
/// with a 90-second job-level heuristic: a CDN throttled to a trickle keeps
/// delivering *a* byte often enough that a whole-request timeout never fires,
/// so the timeout has to sit on the gap between chunks instead.
const CHUNK_TIMEOUT: Duration = Duration::from_secs(20);

/// Upper bound on one segment fetch regardless of how steadily it dribbles in.
const SEGMENT_DEADLINE: Duration = Duration::from_secs(180);

const TEXT_DEADLINE: Duration = Duration::from_secs(30);

#[derive(Clone)]
pub struct HttpClient {
    inner: reqwest::Client,
    user_agent: String,
    referer: Option<String>,
    cancel: CancellationToken,
}

/// What to do about a response, decided in exactly one place.
enum Disposition {
    Success,
    Retry,
    /// vixcloud.co quirk: some episode playlists 403 until `?b=1` is appended.
    RetryWithB1,
    Fatal,
}

fn classify(status: StatusCode, b1_available: bool) -> Disposition {
    if status.is_success() {
        return Disposition::Success;
    }
    match status {
        StatusCode::FORBIDDEN if b1_available => Disposition::RetryWithB1,
        // 403 without the b=1 card left to play is still worth retrying: the
        // CDN hands them out under load, not only for genuinely denied URLs.
        StatusCode::FORBIDDEN
        | StatusCode::REQUEST_TIMEOUT
        | StatusCode::TOO_MANY_REQUESTS
        | StatusCode::INTERNAL_SERVER_ERROR
        | StatusCode::BAD_GATEWAY
        | StatusCode::SERVICE_UNAVAILABLE
        | StatusCode::GATEWAY_TIMEOUT => Disposition::Retry,
        s if s.is_server_error() => Disposition::Retry,
        _ => Disposition::Fatal,
    }
}

/// Append the vixcloud `b=1` escape hatch, respecting an existing query string.
pub fn with_b1(url: &str) -> String {
    if url.contains("b=1") {
        url.to_string()
    } else if url.contains('?') {
        format!("{url}&b=1")
    } else {
        format!("{url}?b=1")
    }
}

/// Exponential backoff with full jitter. Without the jitter, 16 segment workers
/// that hit the same 503 retry in lockstep and reproduce the burst that caused
/// it.
fn backoff(attempt: u32, retry_after: Option<Duration>) -> Duration {
    if let Some(d) = retry_after {
        return d.min(MAX_BACKOFF);
    }
    let ceiling = BASE_BACKOFF_MS.saturating_mul(1u64 << attempt.min(6));
    let jittered = if ceiling == 0 {
        0
    } else {
        rand::random::<u64>() % ceiling
    };
    Duration::from_millis(BASE_BACKOFF_MS / 2 + jittered).min(MAX_BACKOFF)
}

fn retry_after_of(resp: &reqwest::Response) -> Option<Duration> {
    resp.headers()
        .get(reqwest::header::RETRY_AFTER)?
        .to_str()
        .ok()?
        .trim()
        .parse::<u64>()
        .ok()
        .map(Duration::from_secs)
}

impl HttpClient {
    pub fn new(
        user_agent: String,
        referer: Option<String>,
        cancel: CancellationToken,
    ) -> Result<Self> {
        let inner = reqwest::Client::builder()
            // Connections are pooled and reused across all segment workers;
            // opening a fresh TLS session per segment was a measurable share of
            // the old downloader's wall time.
            .pool_max_idle_per_host(32)
            .connect_timeout(Duration::from_secs(15))
            .build()
            .map_err(|source| EngineError::Http {
                context: "creazione client HTTP".into(),
                source,
            })?;
        Ok(Self {
            inner,
            user_agent,
            referer,
            cancel,
        })
    }

    fn request(&self, url: &str) -> reqwest::RequestBuilder {
        let mut req = self
            .inner
            .get(url)
            .header(reqwest::header::USER_AGENT, &self.user_agent);
        if let Some(referer) = &self.referer {
            req = req.header(reqwest::header::REFERER, referer);
        }
        req
    }

    /// Fetch a playlist (or any small text resource) with the full retry policy.
    pub async fn fetch_text(&self, url: &str, context: &str) -> Result<String> {
        let bytes = self.fetch(url, context, TEXT_DEADLINE, true).await?;
        Ok(String::from_utf8_lossy(&bytes).into_owned())
    }

    /// Fetch one media segment. `b=1` is not attempted here: segment URLs come
    /// from a playlist that was already resolved with whatever query string
    /// works, and rewriting them would break signed URLs.
    pub async fn fetch_segment(&self, url: &str, context: &str) -> Result<Vec<u8>> {
        self.fetch(url, context, SEGMENT_DEADLINE, false).await
    }

    async fn fetch(
        &self,
        url: &str,
        context: &str,
        deadline: Duration,
        allow_b1: bool,
    ) -> Result<Vec<u8>> {
        let mut current = url.to_string();
        let mut b1_available = allow_b1 && !url.contains("b=1");
        let mut last_status = StatusCode::OK;

        for attempt in 0..MAX_ATTEMPTS {
            if self.cancel.is_cancelled() {
                return Err(EngineError::Cancelled);
            }

            let send = self.request(&current).send();
            let resp = tokio::select! {
                biased;
                _ = self.cancel.cancelled() => return Err(EngineError::Cancelled),
                r = send => r,
            };

            let mut resp = match resp {
                Ok(r) => r,
                Err(source) => {
                    // Connection reset, DNS hiccup, TLS error: all transient
                    // until proven otherwise by running out of attempts.
                    if attempt + 1 == MAX_ATTEMPTS {
                        return Err(EngineError::Http {
                            context: context.to_string(),
                            source,
                        });
                    }
                    tracing::warn!(
                        "{context}: errore di rete (tentativo {}/{MAX_ATTEMPTS}): {source}",
                        attempt + 1
                    );
                    self.sleep(backoff(attempt, None)).await?;
                    continue;
                }
            };

            last_status = resp.status();
            match classify(last_status, b1_available) {
                Disposition::Success => {
                    return self.read_body(&mut resp, context, deadline).await;
                }
                Disposition::RetryWithB1 => {
                    tracing::warn!("{context}: HTTP 403, riprovo con ?b=1");
                    current = with_b1(&current);
                    b1_available = false;
                    self.sleep(backoff(attempt, None)).await?;
                }
                Disposition::Retry => {
                    if attempt + 1 == MAX_ATTEMPTS {
                        break;
                    }
                    let wait = backoff(attempt, retry_after_of(&resp));
                    tracing::warn!(
                        "{context}: HTTP {} (tentativo {}/{MAX_ATTEMPTS}), riprovo fra {:?}",
                        last_status.as_u16(),
                        attempt + 1,
                        wait
                    );
                    self.sleep(wait).await?;
                }
                Disposition::Fatal => {
                    return Err(EngineError::HttpStatus {
                        context: context.to_string(),
                        status: last_status.as_u16(),
                        attempts: attempt + 1,
                    });
                }
            }
        }

        Err(EngineError::HttpStatus {
            context: context.to_string(),
            status: last_status.as_u16(),
            attempts: MAX_ATTEMPTS,
        })
    }

    /// Read a body chunk by chunk, giving up on the gap between chunks rather
    /// than on total elapsed time — see `CHUNK_TIMEOUT`.
    async fn read_body(
        &self,
        resp: &mut reqwest::Response,
        context: &str,
        deadline: Duration,
    ) -> Result<Vec<u8>> {
        let started = tokio::time::Instant::now();
        let mut buf: Vec<u8> = match resp.content_length() {
            Some(n) if n <= 64 * 1024 * 1024 => Vec::with_capacity(n as usize),
            _ => Vec::new(),
        };

        loop {
            let next = resp.chunk();
            let chunk = tokio::select! {
                biased;
                _ = self.cancel.cancelled() => return Err(EngineError::Cancelled),
                r = tokio::time::timeout(CHUNK_TIMEOUT, next) => r,
            };

            match chunk {
                Err(_elapsed) => {
                    return Err(EngineError::Stalled {
                        context: context.to_string(),
                        secs: CHUNK_TIMEOUT.as_secs(),
                    });
                }
                Ok(Err(source)) => {
                    return Err(EngineError::Http {
                        context: context.to_string(),
                        source,
                    });
                }
                Ok(Ok(None)) => return Ok(buf),
                Ok(Ok(Some(bytes))) => {
                    buf.extend_from_slice(&bytes);
                    if started.elapsed() > deadline {
                        return Err(EngineError::Stalled {
                            context: context.to_string(),
                            secs: deadline.as_secs(),
                        });
                    }
                }
            }
        }
    }

    /// A sleep that a cancellation cuts short. Every wait in the engine goes
    /// through here, so SIGTERM never has to outwait a backoff.
    async fn sleep(&self, d: Duration) -> Result<()> {
        tokio::select! {
            biased;
            _ = self.cancel.cancelled() => Err(EngineError::Cancelled),
            _ = tokio::time::sleep(d) => Ok(()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn b1_respects_existing_query() {
        assert_eq!(with_b1("https://x/y"), "https://x/y?b=1");
        assert_eq!(with_b1("https://x/y?t=1"), "https://x/y?t=1&b=1");
    }

    #[test]
    fn b1_is_not_applied_twice() {
        assert_eq!(with_b1("https://x/y?b=1"), "https://x/y?b=1");
    }

    /// The regression that dropped 2929 segments: every transient server status
    /// must retry, not just 429.
    #[test]
    fn transient_statuses_all_retry() {
        for code in [408u16, 429, 500, 502, 503, 504, 507] {
            let status = StatusCode::from_u16(code).expect("valid status");
            assert!(
                matches!(classify(status, false), Disposition::Retry),
                "HTTP {code} should be retried"
            );
        }
    }

    #[test]
    fn client_errors_are_fatal() {
        for code in [400u16, 401, 404, 410] {
            let status = StatusCode::from_u16(code).expect("valid status");
            assert!(matches!(classify(status, false), Disposition::Fatal));
        }
    }

    #[test]
    fn forbidden_prefers_b1_then_falls_back_to_plain_retry() {
        assert!(matches!(
            classify(StatusCode::FORBIDDEN, true),
            Disposition::RetryWithB1
        ));
        assert!(matches!(
            classify(StatusCode::FORBIDDEN, false),
            Disposition::Retry
        ));
    }

    #[test]
    fn backoff_is_bounded_and_jittered() {
        for attempt in 0..10 {
            let d = backoff(attempt, None);
            assert!(d <= MAX_BACKOFF, "attempt {attempt} exceeded the ceiling");
        }
        assert_eq!(
            backoff(0, Some(Duration::from_secs(3))),
            Duration::from_secs(3)
        );
        // A hostile Retry-After must not park a worker for an hour.
        assert_eq!(backoff(0, Some(Duration::from_secs(9999))), MAX_BACKOFF);
    }
}
