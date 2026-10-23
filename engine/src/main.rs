//! `sc-dl-engine` — the HLS download engine for the StreamingCommunity panel.
//!
//! Reads one `JobSpec` as JSON on stdin, reports newline-delimited events on
//! stdout, logs to stderr, and exits with a code that says what happened.
//!
//! ## Why a separate process
//!
//! The panel used to run downloads on a thread pool inside its own process. A
//! blocking network call cannot be killed from outside its thread in Python, so
//! "cancel" and "force resume" could only *ask* an attempt to stop; a stuck one
//! kept running and would later write its stale status over a newer attempt's.
//! Everything built to contain that — generation counters, per-generation temp
//! directories, hardlink migration between them, "is my generation still
//! current?" guards on every write — exists only because the stop could not be
//! enforced.
//!
//! A child process can simply be killed. That makes cancellation a fact, which
//! is what allows the panel to drop all of the above: there is never more than
//! one live attempt per job, so there is nothing to reconcile.

#![forbid(unsafe_code)]

mod error;
mod ffmpeg;
mod http;
mod lang;
mod playlist;
mod progress;
mod segments;
mod spec;
mod subtitles;

use std::path::{Path, PathBuf};

use tokio::io::AsyncReadExt;
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;

use crate::error::{EngineError, Result};
use crate::progress::{EventSink, Progress};
use crate::spec::{Event, JobSpec, SubtitleMode, exit};

const DEFAULT_USER_AGENT: &str =
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) \
     Chrome/125.0.0.0 Safari/537.36";

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_env("SC_ENGINE_LOG")
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    let (tx, mut rx) = mpsc::unbounded_channel::<Event>();
    // One writer owns stdout, so events are whole lines in order even with
    // sixteen segment workers running.
    let writer = tokio::spawn(async move {
        use std::io::Write;
        let stdout = std::io::stdout();
        while let Some(event) = rx.recv().await {
            let mut handle = stdout.lock();
            match serde_json::to_string(&event) {
                Ok(line) => {
                    let _ = writeln!(handle, "{line}");
                    let _ = handle.flush();
                }
                Err(e) => tracing::error!("evento non serializzabile: {e}"),
            }
        }
    });
    let sink = EventSink::new(tx);

    let cancel = CancellationToken::new();
    install_signal_handlers(cancel.clone());

    let code = match read_spec().await {
        Err(message) => {
            tracing::error!("{message}");
            sink.emit(Event::Error { message });
            exit::BAD_SPEC
        }
        Ok(spec) => match run(&spec, &sink, &cancel).await {
            Ok(output_path) => {
                tracing::info!("completato: {output_path}");
                sink.emit(Event::Done { output_path });
                exit::OK
            }
            Err(EngineError::Cancelled) => {
                tracing::info!("annullato");
                cleanup_partial_output(Path::new(&spec.output_path)).await;
                sink.emit(Event::Cancelled);
                exit::CANCELLED
            }
            Err(e) => {
                tracing::error!("fallito: {e}");
                cleanup_partial_output(Path::new(&spec.output_path)).await;
                sink.emit(Event::Error {
                    message: e.to_string(),
                });
                exit::FAILED
            }
        },
    };

    drop(sink);
    let _ = writer.await;
    std::process::exit(code);
}

/// SIGTERM and SIGINT both mean "stop now, cleanly".
///
/// The panel sends SIGTERM to cancel; if the engine is wedged somewhere it did
/// not anticipate, the panel escalates to SIGKILL after a grace period, and
/// even then nothing is corrupted — a killed engine leaves only `.part` files,
/// which the next attempt ignores.
fn install_signal_handlers(cancel: CancellationToken) {
    tokio::spawn(async move {
        #[cfg(unix)]
        {
            use tokio::signal::unix::{SignalKind, signal};
            let mut term = match signal(SignalKind::terminate()) {
                Ok(s) => s,
                Err(e) => {
                    tracing::error!("impossibile agganciare SIGTERM: {e}");
                    return;
                }
            };
            tokio::select! {
                _ = term.recv() => tracing::info!("ricevuto SIGTERM"),
                _ = tokio::signal::ctrl_c() => tracing::info!("ricevuto SIGINT"),
            }
        }
        #[cfg(not(unix))]
        {
            let _ = tokio::signal::ctrl_c().await;
        }
        cancel.cancel();
    });
}

async fn read_spec() -> std::result::Result<JobSpec, String> {
    let raw = match std::env::args().nth(1) {
        Some(path) if path != "-" => tokio::fs::read_to_string(&path)
            .await
            .map_err(|e| format!("impossibile leggere {path}: {e}"))?,
        _ => {
            let mut buf = String::new();
            tokio::io::stdin()
                .read_to_string(&mut buf)
                .await
                .map_err(|e| format!("impossibile leggere lo spec da stdin: {e}"))?;
            buf
        }
    };
    serde_json::from_str(&raw).map_err(|e| format!("spec non valido: {e}"))
}

/// The whole download, start to finish. Returns the final output path, which
/// is the requested one: the container is the panel's choice, not the engine's.
async fn run(spec: &JobSpec, sink: &EventSink, cancel: &CancellationToken) -> Result<String> {
    let temp_dir = PathBuf::from(&spec.temp_dir);
    let output_path = PathBuf::from(&spec.output_path);
    let stem = output_path
        .file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| "output".to_string());

    if let Some(path) = &spec.ffmpeg_path {
        tracing::info!("ffmpeg: {path}");
        ffmpeg::set_binary(path.clone());
    }

    let content_key = match &spec.key_hex {
        Some(hex) if !hex.trim().is_empty() => Some(playlist::parse_key_hex(hex)?),
        _ => None,
    };

    let http = http::HttpClient::new(
        spec.user_agent
            .clone()
            .unwrap_or_else(|| DEFAULT_USER_AGENT.to_string()),
        spec.referer.clone(),
        cancel.clone(),
    )?;

    // ── Subtitles first ────────────────────────────────────────────────────
    // They are small, and doing them up front means a wrong subtitle URL is
    // discovered in seconds rather than after an hour of video.
    let wanted = resolve_subtitle_specs(&http, spec).await;
    let subtitle_tracks = subtitles::fetch_all(&http, &wanted, &temp_dir, &stem)
        .await
        .unwrap_or_else(|e| {
            tracing::warn!("sottotitoli saltati: {e}");
            Vec::new()
        });

    // ── Video ──────────────────────────────────────────────────────────────
    let video_playlist =
        playlist::resolve_media_playlist(&http, &spec.m3u8_index, content_key, "video").await?;
    if video_playlist.segments.is_empty() {
        return Err(EngineError::Playlist {
            context: spec.m3u8_index.clone(),
            detail: "nessun segmento nella playlist video".into(),
        });
    }
    tracing::info!("video: {} segmenti", video_playlist.segments.len());

    let video_store = segments::SegmentStore::open(temp_dir.join("video")).await?;
    let video_progress = Progress::new(
        sink.clone(),
        Some("video".into()),
        video_playlist.segments.len() as u64,
    );
    let video_files = segments::download_all(
        &http,
        &video_playlist,
        &video_store,
        spec.max_segment_workers,
        cancel,
        &video_progress,
    )
    .await?;

    sink.status("joining");
    // If separate audio renditions are coming, the remux maps video only from
    // this file — so producing an audio stream here would be work spent on
    // something nothing ever reads.
    let video_mode = if spec.audio_tracks.is_empty() {
        ffmpeg::JoinMode::VideoWithAudio
    } else {
        ffmpeg::JoinMode::VideoOnly
    };
    ffmpeg::join_segments(&video_files, &output_path, video_mode, cancel).await?;

    // ── Audio renditions ───────────────────────────────────────────────────
    let mut audio_tracks = Vec::new();
    for (i, track) in spec.audio_tracks.iter().enumerate() {
        let phase = format!("audio_{}", track.language);
        sink.status(&phase);

        let audio_playlist =
            playlist::resolve_media_playlist(&http, &track.url, content_key, &phase).await?;
        if audio_playlist.segments.is_empty() {
            return Err(EngineError::Playlist {
                context: track.url.clone(),
                detail: format!("nessun segmento per la traccia audio {}", track.language),
            });
        }
        tracing::info!(
            "audio {}: {} segmenti",
            track.language,
            audio_playlist.segments.len()
        );

        let store = segments::SegmentStore::open(temp_dir.join(format!("audio_{i}"))).await?;
        let audio_progress = Progress::new(
            sink.clone(),
            Some(phase.clone()),
            audio_playlist.segments.len() as u64,
        );
        let files = segments::download_all(
            &http,
            &audio_playlist,
            &store,
            spec.max_segment_workers,
            cancel,
            &audio_progress,
        )
        .await?;

        let audio_path = temp_dir.join(format!("_audio_{i}.mp4"));
        ffmpeg::join_segments(&files, &audio_path, ffmpeg::JoinMode::AudioOnly, cancel).await?;
        audio_tracks.push(ffmpeg::AudioTrack {
            path: audio_path,
            language: track.language.clone(),
        });
    }

    // ── Final container ────────────────────────────────────────────────────
    // Subtitles kept outside the container are not a merge, only a move, so
    // with no audio renditions there is nothing to announce or remux.
    let embedded: &[ffmpeg::SubtitleTrack] = match spec.subtitle_mode {
        SubtitleMode::Embed => &subtitle_tracks,
        SubtitleMode::External => &[],
    };
    if !audio_tracks.is_empty() || !embedded.is_empty() {
        sink.status("merging");
        ffmpeg::remux(&output_path, &audio_tracks, embedded, cancel).await?;
        // The subtitles are inside the container now; leaving the sidecars
        // behind would make Jellyfin show every track twice.
        for track in embedded {
            let _ = tokio::fs::remove_file(&track.path).await;
        }
    }
    if spec.subtitle_mode == SubtitleMode::External {
        place_beside(&subtitle_tracks, &output_path).await;
    }

    // Only a completed download may clear its scratch space. A failed one keeps
    // it, which is precisely what lets the next attempt resume.
    if let Err(e) = tokio::fs::remove_dir_all(&temp_dir).await {
        tracing::warn!("cartella temporanea non rimossa ({}): {e}", temp_dir.display());
    }

    Ok(output_path.to_string_lossy().into_owned())
}

/// Move the sidecars next to the finished video, Jellyfin's own convention.
///
/// Moved at the end rather than downloaded straight there: written into the
/// library up front they would sit beside a video that does not exist yet, and
/// a scan running mid-download would index a title whose only content is
/// subtitles. Best effort, like every subtitle step: the video is what was
/// asked for. Must run before the temp dir is cleared, which is where they are.
async fn place_beside(tracks: &[ffmpeg::SubtitleTrack], video: &Path) {
    let Some(dir) = video.parent() else { return };
    for track in tracks {
        let Some(name) = track.path.file_name() else { continue };
        let target = dir.join(name);
        // A rename cannot cross filesystems, and in Docker the temp dir and the
        // library routinely are different ones.
        let moved = match tokio::fs::rename(&track.path, &target).await {
            Ok(()) => Ok(()),
            Err(_) => match tokio::fs::copy(&track.path, &target).await {
                Ok(_) => tokio::fs::remove_file(&track.path).await,
                Err(e) => Err(e),
            },
        };
        match moved {
            Ok(()) => tracing::info!("sottotitolo accanto al video: {}", target.display()),
            Err(e) => tracing::warn!("sottotitolo {} non spostato: {e}", target.display()),
        }
    }
}

/// Merge the subtitle playlists the panel resolved with those the master
/// playlist advertises, preferring the panel's when both name a language.
///
/// A subtitle that cannot be discovered is a warning, never a failure: the
/// download the user actually asked for is the video.
async fn resolve_subtitle_specs(
    http: &http::HttpClient,
    spec: &JobSpec,
) -> Vec<spec::SubtitleTrackSpec> {
    let mut wanted = spec.subtitle_tracks.clone();
    if spec.subtitle_languages.is_empty() {
        return wanted;
    }

    let body = match http.fetch_text(&spec.m3u8_index, "master (sottotitoli)").await {
        Ok(body) => body,
        Err(e) => {
            tracing::warn!("master playlist non leggibile per i sottotitoli: {e}");
            return wanted;
        }
    };

    match playlist::subtitle_alternatives(&body, &spec.m3u8_index) {
        Ok(found) => {
            for (language, uri) in found {
                if !spec.subtitle_languages.contains(&language) {
                    continue;
                }
                if wanted.iter().any(|t| t.language == language) {
                    continue;
                }
                tracing::info!("sottotitolo {language} trovato nel master playlist");
                wanted.push(spec::SubtitleTrackSpec { uri, language });
            }
        }
        Err(e) => tracing::warn!("sottotitoli non estraibili dal master playlist: {e}"),
    }
    wanted
}

/// Delete a half-written output so nothing downstream mistakes it for a
/// finished file. Segment scratch space is deliberately left alone.
async fn cleanup_partial_output(output_path: &Path) {
    // Never the same title in the other container: that is a finished file
    // from an earlier setting, not something this attempt wrote.
    for candidate in [output_path.to_path_buf(), ffmpeg::staging_path(output_path)] {
        if tokio::fs::remove_file(&candidate).await.is_ok() {
            tracing::info!("rimosso output parziale: {}", candidate.display());
        }
    }
    // Remove the containing folder only if this download was the only thing in
    // it — `remove_dir` fails on a non-empty directory, which is the check.
    if let Some(parent) = output_path.parent() {
        let _ = tokio::fs::remove_dir(parent).await;
    }
}
