//! Muxing: concatenating segments into a playable file, and the final remux.
//!
//! The segments are streamed into ffmpeg's stdin instead of being concatenated
//! into a `_combined.ts` on disk first. That halves the I/O for a multi-gigabyte
//! film, and — more to the point — it deletes the file that caused a production
//! crash: a half-written `_combined.ts` left in the segment directory by an
//! interrupted attempt was picked up by the next attempt's directory listing
//! and treated as segment number zero.

use std::path::{Path, PathBuf};
use std::process::Stdio;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::process::{Child, Command};
use tokio_util::sync::CancellationToken;

use crate::error::{EngineError, IoContext, Result};
use crate::lang;

/// Only the tail of ffmpeg's stderr is worth surfacing; it is verbose and the
/// last lines carry the actual complaint.
const STDERR_KEEP: usize = 1200;

/// The ffmpeg binary, chosen once at startup by the caller.
static FFMPEG_BIN: std::sync::OnceLock<String> = std::sync::OnceLock::new();

/// Record which ffmpeg to run. Ignored if called more than once.
pub fn set_binary(path: String) {
    let _ = FFMPEG_BIN.set(path);
}

fn binary() -> &'static str {
    FFMPEG_BIN.get().map(String::as_str).unwrap_or("ffmpeg")
}

#[derive(Debug, Clone)]
pub struct AudioTrack {
    pub path: PathBuf,
    pub language: String,
}

#[derive(Debug, Clone)]
pub struct SubtitleTrack {
    pub path: PathBuf,
    pub language: String,
}

fn spawn(args: &[&str], stage: &str) -> Result<Child> {
    Command::new(binary())
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .map_err(|e| {
            if e.kind() == std::io::ErrorKind::NotFound {
                EngineError::FfmpegMissing
            } else {
                EngineError::Ffmpeg {
                    stage: stage.to_string(),
                    code: "spawn".into(),
                    stderr: e.to_string(),
                }
            }
        })
}

fn tail(buf: &[u8]) -> String {
    let text = String::from_utf8_lossy(buf);
    let trimmed = text.trim();
    if trimmed.len() <= STDERR_KEEP {
        return trimmed.to_string();
    }
    let start = trimmed.len() - STDERR_KEEP;
    // Never split a UTF-8 sequence.
    let start = (start..trimmed.len())
        .find(|i| trimmed.is_char_boundary(*i))
        .unwrap_or(trimmed.len());
    format!("...{}", &trimmed[start..])
}

/// What the joined output is actually for, which decides how much work ffmpeg
/// has to do.
///
/// This matters more than it looks. The old code re-encoded audio to AAC during
/// *every* join, including the video join of a title that has separate audio
/// renditions — where the remux then drops that audio stream entirely
/// (`-map 0:v:0`). On a two-hour film that is minutes of CPU spent producing a
/// stream that is thrown away.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JoinMode {
    /// The video carries the only audio there is: keep and normalise it.
    VideoWithAudio,
    /// Separate audio renditions will replace this file's audio, so don't
    /// bother producing any.
    VideoOnly,
    /// An audio rendition. The remux copies it, so copy it here too.
    AudioOnly,
}

impl JoinMode {
    fn codec_args(self) -> &'static [&'static str] {
        match self {
            JoinMode::VideoWithAudio => &[
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-af",
                "aresample=async=1000",
            ],
            JoinMode::VideoOnly => &["-c:v", "copy", "-an"],
            // HLS audio renditions are already AAC, so this is a remux, not a
            // transcode. `retry_codec_args` covers the rare stream that is not.
            JoinMode::AudioOnly => &["-vn", "-c:a", "copy"],
        }
    }

    /// A second attempt for a stream that could not simply be copied.
    fn retry_codec_args(self) -> Option<&'static [&'static str]> {
        match self {
            JoinMode::AudioOnly => Some(&["-vn", "-c:a", "aac", "-b:a", "192k"]),
            _ => None,
        }
    }
}

/// Concatenate `segments` (in order) into `output`.
///
/// Byte-level concatenation is deliberate: MPEG-TS is a continuous stream
/// format, so feeding the raw bytes through lets ffmpeg's TS demuxer recover
/// PCR/PTS continuity itself, which the concat demuxer does not do as well.
pub async fn join_segments(
    segments: &[PathBuf],
    output: &Path,
    mode: JoinMode,
    cancel: &CancellationToken,
) -> Result<()> {
    let started = std::time::Instant::now();
    let outcome = join_once(segments, output, mode.codec_args(), cancel).await;

    let outcome = match (&outcome, mode.retry_codec_args()) {
        (Err(EngineError::Ffmpeg { .. }), Some(fallback)) => {
            tracing::warn!("join {mode:?}: copia diretta rifiutata, riprovo ricodificando");
            join_once(segments, output, fallback, cancel).await
        }
        _ => outcome,
    };

    if outcome.is_ok() {
        tracing::info!(
            "join {mode:?} completato in {:.1}s ({} segmenti)",
            started.elapsed().as_secs_f64(),
            segments.len()
        );
    }
    outcome
}

async fn join_once(
    segments: &[PathBuf],
    output: &Path,
    codec_args: &[&str],
    cancel: &CancellationToken,
) -> Result<()> {
    if let Some(parent) = output.parent() {
        tokio::fs::create_dir_all(parent).await.at(parent)?;
    }
    let output_str = output.to_string_lossy().into_owned();

    let mut args: Vec<&str> = vec![
        "-hide_banner",
        "-loglevel",
        "error",
        // Piped input cannot be re-read, so ffmpeg needs enough of a window to
        // identify the streams before it commits. `analyzeduration` is in
        // microseconds: 10M is ten seconds of stream, which is plenty for a TS
        // and far cheaper than the 100s the first cut of this asked for.
        "-probesize",
        "20M",
        "-analyzeduration",
        "10M",
        "-fflags",
        "+genpts",
        "-avoid_negative_ts",
        "make_zero",
        "-i",
        "pipe:0",
    ];
    args.extend_from_slice(codec_args);
    args.extend_from_slice(&["-movflags", "+faststart", "-y", &output_str]);

    let mut child = spawn(&args, "join")?;

    let stdin = child.stdin.take().ok_or_else(|| EngineError::Ffmpeg {
        stage: "join".into(),
        code: "pipe".into(),
        stderr: "stdin non disponibile".into(),
    })?;
    let mut stderr = child.stderr.take().ok_or_else(|| EngineError::Ffmpeg {
        stage: "join".into(),
        code: "pipe".into(),
        stderr: "stderr non disponibile".into(),
    })?;

    // stderr must be drained while writing, or a chatty ffmpeg fills its pipe
    // buffer and both processes block forever.
    let stderr_task = tokio::spawn(async move {
        let mut buf = Vec::new();
        let _ = stderr.read_to_end(&mut buf).await;
        buf
    });

    // `stdin` is *moved* in here on purpose. ffmpeg only sees EOF when the write
    // end of the pipe is closed, and for `ChildStdin` that happens on drop —
    // `shutdown()` alone leaves the descriptor open and both processes wait for
    // each other forever. Owning it here means every exit from this block,
    // including a cancellation that drops the future, closes the pipe.
    let feed = async move {
        let mut stdin = stdin;
        let mut copy_buf = vec![0u8; 1 << 20];
        for path in segments {
            let mut file = tokio::fs::File::open(path).await.at(path)?;
            loop {
                let read = file.read(&mut copy_buf).await.at(path)?;
                if read == 0 {
                    break;
                }
                // A broken pipe here means ffmpeg exited early; its stderr
                // explains why, so stop feeding and let the exit status talk.
                if stdin.write_all(&copy_buf[..read]).await.is_err() {
                    return Ok(false);
                }
            }
        }
        let _ = stdin.flush().await;
        drop(stdin);
        Ok::<bool, EngineError>(true)
    };

    let fed = tokio::select! {
        biased;
        _ = cancel.cancelled() => {
            let _ = child.kill().await;
            let _ = stderr_task.await;
            return Err(EngineError::Cancelled);
        }
        result = feed => result,
    };
    // `stdin` is dropped by now either way, closing the pipe so ffmpeg can
    // finish; propagate a genuine read error only after that.
    let fed = fed?;

    let status = tokio::select! {
        biased;
        _ = cancel.cancelled() => {
            let _ = child.kill().await;
            let _ = stderr_task.await;
            return Err(EngineError::Cancelled);
        }
        status = child.wait() => status.map_err(|e| EngineError::Ffmpeg {
            stage: "join".into(),
            code: "wait".into(),
            stderr: e.to_string(),
        })?,
    };

    let stderr_bytes = stderr_task.await.unwrap_or_default();

    if !status.success() {
        return Err(EngineError::Ffmpeg {
            stage: "join".into(),
            code: status
                .code()
                .map(|c| c.to_string())
                .unwrap_or_else(|| "segnale".into()),
            stderr: tail(&stderr_bytes),
        });
    }
    if !fed {
        return Err(EngineError::Ffmpeg {
            stage: "join".into(),
            code: "EPIPE".into(),
            stderr: format!(
                "ffmpeg ha chiuso l'input prima della fine dei segmenti: {}",
                tail(&stderr_bytes)
            ),
        });
    }
    Ok(())
}

/// Build the argument list for the final remux.
///
/// Split out from the process handling so the mapping — the part that has
/// actually been wrong before — is testable without running ffmpeg. The
/// per-stream `-map`/`-metadata`/`-disposition` options are *output* options
/// and must appear after every `-i` but before the output filename; putting
/// them anywhere else makes ffmpeg ignore them silently, which is how subtitle
/// language tags went missing for a while.
fn remux_args(
    video: &Path,
    audio: &[AudioTrack],
    subtitles: &[SubtitleTrack],
    output: &Path,
) -> Vec<String> {
    let mut args: Vec<String> = vec![
        "-hide_banner".into(),
        "-loglevel".into(),
        "error".into(),
        "-y".into(),
        "-i".into(),
        video.to_string_lossy().into_owned(),
    ];
    for track in audio {
        args.push("-i".into());
        args.push(track.path.to_string_lossy().into_owned());
    }
    for track in subtitles {
        args.push("-i".into());
        args.push(track.path.to_string_lossy().into_owned());
    }

    args.push("-map".into());
    args.push("0:v:0".into());
    if audio.is_empty() {
        // Keep whatever audio the video already carries.
        args.push("-map".into());
        args.push("0:a?".into());
    } else {
        for i in 0..audio.len() {
            args.push("-map".into());
            args.push(format!("{}:a:0", i + 1));
        }
    }
    for i in 0..subtitles.len() {
        args.push("-map".into());
        args.push(format!("{}:0", audio.len() + 1 + i));
    }

    // Matroska carries WebVTT natively, so nothing needs re-encoding.
    args.extend(
        ["-c:v", "copy", "-c:a", "copy", "-c:s", "copy"]
            .iter()
            .map(|s| s.to_string()),
    );

    for (i, track) in audio.iter().enumerate() {
        let (code, _) = lang::normalize(&track.language);
        args.push(format!("-metadata:s:a:{i}"));
        args.push(format!("language={code}"));
    }
    for (i, track) in subtitles.iter().enumerate() {
        let (code, forced) = lang::normalize(&track.language);
        let mut name = lang::display_name(code);
        if forced {
            name.push_str(" (Forced)");
        }
        args.push(format!("-metadata:s:s:{i}"));
        args.push(format!("language={code}"));
        args.push(format!("-metadata:s:s:{i}"));
        args.push(format!("title={name}"));
        args.push(format!("-disposition:s:{i}"));
        args.push(if forced { "forced".into() } else { "0".into() });
    }

    args.push(output.to_string_lossy().into_owned());
    args
}

/// Remux video + external audio/subtitle tracks into a single `.mkv`.
///
/// Returns the path actually produced: the untouched input when there is
/// nothing to add.
pub async fn remux_to_mkv(
    video: &Path,
    audio: &[AudioTrack],
    subtitles: &[SubtitleTrack],
    cancel: &CancellationToken,
) -> Result<PathBuf> {
    if audio.is_empty() && subtitles.is_empty() {
        return Ok(video.to_path_buf());
    }

    let output = video.with_extension("mkv");
    let args = remux_args(video, audio, subtitles, &output);
    let borrowed: Vec<&str> = args.iter().map(|s| s.as_str()).collect();

    tracing::info!(
        "remux MKV: video + {} tracce audio + {} sottotitoli",
        audio.len(),
        subtitles.len()
    );

    let mut child = spawn(&borrowed, "remux")?;
    let mut stderr = child.stderr.take().ok_or_else(|| EngineError::Ffmpeg {
        stage: "remux".into(),
        code: "pipe".into(),
        stderr: "stderr non disponibile".into(),
    })?;
    drop(child.stdin.take());

    let stderr_task = tokio::spawn(async move {
        let mut buf = Vec::new();
        let _ = stderr.read_to_end(&mut buf).await;
        buf
    });

    let status = tokio::select! {
        biased;
        _ = cancel.cancelled() => {
            let _ = child.kill().await;
            let _ = stderr_task.await;
            // A partial .mkv would be mistaken for the finished article.
            let _ = tokio::fs::remove_file(&output).await;
            return Err(EngineError::Cancelled);
        }
        status = child.wait() => status.map_err(|e| EngineError::Ffmpeg {
            stage: "remux".into(),
            code: "wait".into(),
            stderr: e.to_string(),
        })?,
    };

    let stderr_bytes = stderr_task.await.unwrap_or_default();
    if !status.success() {
        let _ = tokio::fs::remove_file(&output).await;
        return Err(EngineError::Ffmpeg {
            stage: "remux".into(),
            code: status
                .code()
                .map(|c| c.to_string())
                .unwrap_or_else(|| "segnale".into()),
            stderr: tail(&stderr_bytes),
        });
    }

    if output != video {
        let _ = tokio::fs::remove_file(video).await;
    }
    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args_of(audio: &[AudioTrack], subs: &[SubtitleTrack]) -> Vec<String> {
        remux_args(
            Path::new("/lib/Film.mp4"),
            audio,
            subs,
            Path::new("/lib/Film.mkv"),
        )
    }

    fn audio(language: &str) -> AudioTrack {
        AudioTrack {
            path: PathBuf::from(format!("/tmp/{language}.mp4")),
            language: language.to_string(),
        }
    }

    fn subtitle(language: &str) -> SubtitleTrack {
        SubtitleTrack {
            path: PathBuf::from(format!("/tmp/{language}.vtt")),
            language: language.to_string(),
        }
    }

    /// Every per-stream option must sit after the last `-i` and before the
    /// output path, or ffmpeg drops it without complaining.
    #[test]
    fn per_stream_options_land_between_the_inputs_and_the_output() {
        let args = args_of(&[audio("ita")], &[subtitle("eng")]);
        let last_input = args
            .iter()
            .rposition(|a| a == "-i")
            .expect("there is an input");
        let first_metadata = args
            .iter()
            .position(|a| a.starts_with("-metadata:s:"))
            .expect("there is metadata");
        assert!(first_metadata > last_input);
        assert_eq!(args.last().map(String::as_str), Some("/lib/Film.mkv"));
    }

    #[test]
    fn stream_indices_account_for_every_preceding_input() {
        let args = args_of(&[audio("ita"), audio("eng")], &[subtitle("ita")]);
        let maps: Vec<&String> = args
            .iter()
            .enumerate()
            .filter(|(i, _)| i > &0 && args[i - 1] == "-map")
            .map(|(_, a)| a)
            .collect();
        // video, two audio inputs (1,2), then the subtitle input (3)
        assert_eq!(maps, vec!["0:v:0", "1:a:0", "2:a:0", "3:0"]);
    }

    #[test]
    fn without_external_audio_the_original_track_is_kept() {
        let args = args_of(&[], &[subtitle("ita")]);
        assert!(args.windows(2).any(|w| w[0] == "-map" && w[1] == "0:a?"));
        // The single subtitle is then input 1, not input 0.
        assert!(args.windows(2).any(|w| w[0] == "-map" && w[1] == "1:0"));
    }

    /// `forced-ita` must become a real language plus a disposition, never a
    /// literal language code that players render as "Undefined".
    #[test]
    fn forced_subtitles_get_a_code_a_title_and_a_disposition() {
        let args = args_of(&[], &[subtitle("forced-ita")]);
        assert!(args.contains(&"language=ita".to_string()));
        assert!(args.contains(&"title=Italian (Forced)".to_string()));
        let disposition = args
            .iter()
            .position(|a| a == "-disposition:s:0")
            .expect("disposition present");
        assert_eq!(args[disposition + 1], "forced");
    }

    #[test]
    fn ordinary_subtitles_get_a_cleared_disposition() {
        let args = args_of(&[], &[subtitle("eng")]);
        let disposition = args
            .iter()
            .position(|a| a == "-disposition:s:0")
            .expect("disposition present");
        assert_eq!(args[disposition + 1], "0");
    }

    /// The waste this mode exists to remove: when separate audio renditions
    /// will replace it, the video join must not spend minutes encoding an AAC
    /// stream that the remux then drops with `-map 0:v:0`.
    #[test]
    fn a_video_join_that_will_be_re_audioed_encodes_no_audio() {
        let args = JoinMode::VideoOnly.codec_args();
        assert!(args.contains(&"-an"), "{args:?}");
        assert!(!args.contains(&"aac"), "no transcode should be requested");
    }

    #[test]
    fn a_video_join_that_keeps_its_own_audio_still_normalises_it() {
        let args = JoinMode::VideoWithAudio.codec_args();
        assert!(args.contains(&"aac"));
        assert!(args.contains(&"aresample=async=1000"));
        assert!(!args.contains(&"-an"));
    }

    /// The remux copies audio renditions, so joining them should copy too —
    /// with a re-encode held in reserve for a stream mp4 will not take.
    #[test]
    fn an_audio_join_copies_first_and_can_fall_back() {
        let args = JoinMode::AudioOnly.codec_args();
        assert_eq!(args, &["-vn", "-c:a", "copy"]);

        let fallback = JoinMode::AudioOnly
            .retry_codec_args()
            .expect("a copy that fails must have somewhere to go");
        assert!(fallback.contains(&"aac"));

        // A video join has no fallback: re-running it would cost as much as the
        // original attempt and fail the same way.
        assert!(JoinMode::VideoOnly.retry_codec_args().is_none());
        assert!(JoinMode::VideoWithAudio.retry_codec_args().is_none());
    }

    #[test]
    fn stderr_tail_is_bounded_and_utf8_safe() {
        let noisy = "à".repeat(4000);
        let out = tail(noisy.as_bytes());
        assert!(out.len() <= STDERR_KEEP + 8);
        assert!(out.starts_with("..."));
    }
}
