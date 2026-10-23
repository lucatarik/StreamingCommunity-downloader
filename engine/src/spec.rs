//! The process boundary: what the panel sends in, what the engine reports back.
//!
//! The engine is deliberately a *separate process* rather than a library. A
//! blocking download inside the Python panel could not be killed from outside
//! its thread, which is the root cause of every concurrency workaround in
//! `app/jobs.py` (generation counters, per-generation temp dirs, zombie
//! attempts writing stale state). A child process is killable by the OS, so
//! cancellation becomes a fact instead of a hope.

use serde::{Deserialize, Serialize};

fn default_workers() -> usize {
    16
}

/// One download, described entirely up front.
///
/// Everything the engine needs arrives in this struct: it never scrapes, never
/// reads the panel's config files, and never decides where a file belongs. The
/// Python side owns all of that and hands over resolved URLs and paths.
#[derive(Debug, Clone, Deserialize)]
pub struct JobSpec {
    /// Master playlist, or a media playlist directly — the engine detects which.
    pub m3u8_index: String,

    /// AES-128 content key, hex encoded. Only used when the playlist actually
    /// carries an `EXT-X-KEY`; a key supplied for an unencrypted stream is
    /// ignored rather than applied (applying it would corrupt every segment).
    #[serde(default)]
    pub key_hex: Option<String>,

    #[serde(default)]
    pub referer: Option<String>,

    /// Final destination. Its extension is the container (`.mkv` or `.mp4`),
    /// chosen by the panel's `output_container` setting; the engine never
    /// changes it, so `Done.output_path` is always this path.
    pub output_path: String,

    /// Scratch space. Segments live here and are deliberately *kept* on
    /// failure so a later attempt resumes instead of restarting.
    pub temp_dir: String,

    #[serde(default)]
    pub audio_tracks: Vec<AudioTrackSpec>,

    /// Subtitle playlists the panel already resolved. Merged with whatever the
    /// master playlist advertises for `subtitle_languages`.
    #[serde(default)]
    pub subtitle_tracks: Vec<SubtitleTrackSpec>,

    /// Languages to look for in the master playlist's own subtitle renditions.
    #[serde(default)]
    pub subtitle_languages: Vec<String>,

    /// Whether subtitles go inside the container or beside it as
    /// `<stem>.<lang>.vtt`, Jellyfin's sidecar convention.
    #[serde(default)]
    pub subtitle_mode: SubtitleMode,

    #[serde(default = "default_workers")]
    pub max_segment_workers: usize,

    #[serde(default)]
    pub user_agent: Option<String>,

    /// The ffmpeg binary to use. The panel resolves this through its own seam
    /// (`app/core/ffmpeg_path.py`), which falls back to a bundled static build
    /// where the platform has no package manager to install one. Falling back
    /// to a bare PATH lookup here would quietly diverge from that.
    #[serde(default)]
    pub ffmpeg_path: Option<String>,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SubtitleMode {
    #[default]
    Embed,
    External,
}

#[derive(Debug, Clone, Deserialize)]
pub struct AudioTrackSpec {
    /// Media (or master) playlist URL for this audio rendition.
    pub url: String,
    #[serde(default = "und")]
    pub language: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct SubtitleTrackSpec {
    /// The subtitle *playlist* URI — the WebVTT file is one level below it.
    pub uri: String,
    #[serde(default = "und")]
    pub language: String,
}

fn und() -> String {
    "und".to_string()
}

/// Everything the engine says, as newline-delimited JSON on stdout.
///
/// The shapes mirror what `app/progress.py`'s `WebProgressBar` already emits so
/// the Python bridge can forward them to the SSE stream untouched. Human
/// -readable logging goes to stderr and is never interleaved here.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Event {
    Progress {
        #[serde(skip_serializing_if = "Option::is_none")]
        phase: Option<String>,
        current: u64,
        total: u64,
        pct: f64,
        /// Segments per second.
        speed: f64,
        /// Bytes per second.
        bytes_speed: u64,
        /// Bytes fetched so far in this phase. The panel's progress bar wants
        /// deltas, not rates, so it can keep its own running totals and the
        /// size estimate it extrapolates from them.
        bytes: u64,
        #[serde(skip_serializing_if = "Option::is_none")]
        eta: Option<u64>,
    },
    /// A phase transition with no numeric progress of its own ("joining",
    /// "merging", "audio_ita", ...).
    Status {
        phase: String,
    },
    Done {
        output_path: String,
    },
    Error {
        message: String,
    },
    /// Clean stop after SIGTERM/SIGINT. Distinct from `Error` so the panel can
    /// tell "the user cancelled" from "this download is broken" without
    /// string-matching a message, which is what `app/static/app.js` had to do.
    Cancelled,
}

/// Process exit codes. The panel can act on these without parsing stdout.
pub mod exit {
    pub const OK: i32 = 0;
    pub const FAILED: i32 = 1;
    /// Bad spec on stdin — a programming error in the caller, not a download
    /// failure worth retrying.
    pub const BAD_SPEC: i32 = 2;
    pub const CANCELLED: i32 = 130;
}
