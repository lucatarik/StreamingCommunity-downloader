//! WebVTT subtitle fetching.
//!
//! Subtitles are a best-effort extra: a missing one is logged and skipped, never
//! a reason to fail a download the user asked for. That matches the panel's
//! existing behaviour — and it is the opposite of the policy for *audio*, where
//! a missing requested language is a hard error, because silently handing
//! someone an English-only file is worse than telling them it could not be had.

use std::path::{Path, PathBuf};

use crate::error::{IoContext, Result};
use crate::http::HttpClient;
use crate::lang;
use crate::playlist;
use crate::spec::SubtitleTrackSpec;

/// Filename Jellyfin recognises as a sidecar for `<stem>`.
pub fn sidecar_name(stem: &str, language: &str) -> String {
    let (code, forced) = lang::normalize(language);
    let short = lang::short_code(code);
    if forced {
        format!("{stem}.{short}.forced.vtt")
    } else {
        format!("{stem}.{short}.vtt")
    }
}

/// Download every requested subtitle into `dir`, returning what actually landed.
pub async fn fetch_all(
    http: &HttpClient,
    tracks: &[SubtitleTrackSpec],
    dir: &Path,
    stem: &str,
) -> Result<Vec<crate::ffmpeg::SubtitleTrack>> {
    if tracks.is_empty() {
        return Ok(Vec::new());
    }
    tokio::fs::create_dir_all(dir).await.at(dir)?;

    let mut out = Vec::new();
    let mut seen: Vec<PathBuf> = Vec::new();

    for track in tracks {
        let path = dir.join(sidecar_name(stem, &track.language));
        if seen.contains(&path) {
            continue;
        }

        match fetch_one(http, &track.uri, &path).await {
            Ok(()) => {
                tracing::info!("sottotitolo {} scaricato in {}", track.language, path.display());
                seen.push(path.clone());
                out.push(crate::ffmpeg::SubtitleTrack {
                    path,
                    language: track.language.clone(),
                });
            }
            Err(e) => {
                tracing::warn!("sottotitolo {} non disponibile: {e}", track.language);
                // Do not leave a truncated .vtt where a valid one is expected.
                let _ = tokio::fs::remove_file(&path).await;
            }
        }
    }

    Ok(out)
}

async fn fetch_one(http: &HttpClient, playlist_uri: &str, dest: &Path) -> Result<()> {
    let context = format!("playlist sottotitoli {playlist_uri}");
    let body = http.fetch_text(playlist_uri, &context).await?;
    let parsed = playlist::parse_media(&body, playlist_uri, None)?;

    let vtt_url = parsed
        .subtitles
        .first()
        .or_else(|| parsed.segments.first())
        .ok_or_else(|| crate::error::EngineError::Playlist {
            context: playlist_uri.to_string(),
            detail: "nessun file WebVTT nella playlist".into(),
        })?;

    let content = http.fetch_text(vtt_url, "file WebVTT").await?;
    // Same atomic-write discipline as the segments: a `.vtt` that exists is a
    // `.vtt` that is complete, so the remux never embeds a half file.
    let part = dest.with_extension("vtt.part");
    tokio::fs::write(&part, content.as_bytes()).await.at(&part)?;
    tokio::fs::rename(&part, dest).await.at(dest)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sidecar_names_follow_the_jellyfin_convention() {
        assert_eq!(sidecar_name("Film (2020)", "ita"), "Film (2020).it.vtt");
        assert_eq!(sidecar_name("Film (2020)", "eng"), "Film (2020).en.vtt");
    }

    /// A forced track must not collide with the ordinary one for the same
    /// language — one would overwrite the other on disk.
    #[test]
    fn forced_tracks_get_their_own_filename() {
        assert_ne!(
            sidecar_name("Film", "forced-ita"),
            sidecar_name("Film", "ita")
        );
        assert_eq!(sidecar_name("Film", "forced-ita"), "Film.it.forced.vtt");
    }
}
