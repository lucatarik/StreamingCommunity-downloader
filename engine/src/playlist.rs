//! M3U8 parsing and rendition resolution.
//!
//! Segment URIs are resolved to absolute URLs *here*, once, against the URL the
//! playlist was actually fetched from. The Python version passed relative URIs
//! straight to `requests`, which happened to work only because this particular
//! CDN always emits absolute ones.

use m3u8_rs::{KeyMethod, Playlist};
use url::Url;

use crate::error::{EngineError, Result};
use crate::http::HttpClient;

/// How a media playlist's segments are encrypted, if at all.
#[derive(Debug, Clone)]
pub enum Encryption {
    None,
    /// AES-128 CBC. `iv` is `None` when the playlist omits it, in which case
    /// the per-segment sequence number is used, as the HLS spec requires.
    ///
    /// The Python version simply skipped decryption when the IV attribute was
    /// missing, writing ciphertext to disk as though it were video — a silent
    /// corruption that only showed up as an unplayable file.
    Aes128 { key: [u8; 16], iv: Option<[u8; 16]> },
}

#[derive(Debug, Clone)]
pub struct MediaPlaylist {
    /// Absolute segment URLs, in playback order.
    pub segments: Vec<String>,
    /// Absolute WebVTT URLs, for subtitle playlists.
    pub subtitles: Vec<String>,
    pub encryption: Encryption,
    /// `EXT-X-MEDIA-SEQUENCE`, needed to derive an implicit IV.
    pub media_sequence: u64,
}

#[derive(Debug, Clone)]
pub struct Variant {
    pub uri: String,
    pub bandwidth: u64,
    pub resolution: Option<String>,
}

fn absolutize(base: &Url, uri: &str) -> Result<String> {
    base.join(uri)
        .map(|u| u.to_string())
        .map_err(|e| EngineError::Playlist {
            context: format!("URI {uri}"),
            detail: e.to_string(),
        })
}

fn parse_hex_iv(raw: &str) -> Option<[u8; 16]> {
    let cleaned = raw.trim().trim_start_matches("0x").trim_start_matches("0X");
    if cleaned.len() != 32 {
        return None;
    }
    let mut out = [0u8; 16];
    for (i, byte) in out.iter_mut().enumerate() {
        *byte = u8::from_str_radix(cleaned.get(i * 2..i * 2 + 2)?, 16).ok()?;
    }
    Some(out)
}

pub fn parse_key_hex(raw: &str) -> Result<[u8; 16]> {
    let cleaned = raw.trim().trim_start_matches("0x").trim_start_matches("0X");
    if cleaned.len() != 32 {
        return Err(EngineError::BadKey(format!(
            "attesi 32 caratteri esadecimali, ricevuti {}",
            cleaned.len()
        )));
    }
    let mut out = [0u8; 16];
    for (i, byte) in out.iter_mut().enumerate() {
        let pair = cleaned
            .get(i * 2..i * 2 + 2)
            .ok_or_else(|| EngineError::BadKey("chiave troncata".into()))?;
        *byte = u8::from_str_radix(pair, 16)
            .map_err(|e| EngineError::BadKey(format!("{pair}: {e}")))?;
    }
    Ok(out)
}

/// Parse a media playlist, resolving URIs against `base_url`.
///
/// `content_key` is the key material the panel scraped out of the page; it is
/// applied **only** when the playlist itself declares AES-128. A key handed to
/// an unencrypted stream is ignored rather than used, because "decrypting"
/// plaintext produces a file that looks fine on disk and plays as noise.
pub fn parse_media(
    body: &str,
    base_url: &str,
    content_key: Option<[u8; 16]>,
) -> Result<MediaPlaylist> {
    let base = Url::parse(base_url).map_err(|e| EngineError::Playlist {
        context: base_url.to_string(),
        detail: e.to_string(),
    })?;

    let parsed = m3u8_rs::parse_playlist_res(body.as_bytes()).map_err(|_| {
        EngineError::Playlist {
            context: base_url.to_string(),
            detail: preview(body),
        }
    })?;

    let media = match parsed {
        Playlist::MediaPlaylist(m) => m,
        Playlist::MasterPlaylist(_) => {
            return Err(EngineError::Playlist {
                context: base_url.to_string(),
                detail: "atteso un media playlist, ricevuto un master playlist".into(),
            });
        }
    };

    let mut segments = Vec::with_capacity(media.segments.len());
    let mut subtitles = Vec::new();
    let mut encryption = Encryption::None;

    for segment in &media.segments {
        if let Some(key) = &segment.key {
            match key.method {
                KeyMethod::AES128 => {
                    if let Some(content_key) = content_key {
                        encryption = Encryption::Aes128 {
                            key: content_key,
                            iv: key.iv.as_deref().and_then(parse_hex_iv),
                        };
                    } else {
                        return Err(EngineError::Playlist {
                            context: base_url.to_string(),
                            detail: "playlist cifrata (AES-128) ma nessuna chiave fornita".into(),
                        });
                    }
                }
                KeyMethod::None => {}
                KeyMethod::SampleAES | KeyMethod::Other(_) => {
                    return Err(EngineError::Playlist {
                        context: base_url.to_string(),
                        detail: format!("metodo di cifratura non supportato: {:?}", key.method),
                    });
                }
            }
        }

        let absolute = absolutize(&base, &segment.uri)?;
        // WebVTT "segments" appear in subtitle playlists, where they are the
        // payload rather than video.
        if segment.uri.contains(".vtt") {
            subtitles.push(absolute);
        } else {
            segments.push(absolute);
        }
    }

    Ok(MediaPlaylist {
        segments,
        subtitles,
        encryption,
        media_sequence: media.media_sequence,
    })
}

/// Highest-bandwidth variant of a master playlist.
pub fn best_variant(body: &str, base_url: &str) -> Result<Option<Variant>> {
    let base = Url::parse(base_url).map_err(|e| EngineError::Playlist {
        context: base_url.to_string(),
        detail: e.to_string(),
    })?;

    let parsed = m3u8_rs::parse_playlist_res(body.as_bytes()).map_err(|_| {
        EngineError::Playlist {
            context: base_url.to_string(),
            detail: preview(body),
        }
    })?;

    let master = match parsed {
        Playlist::MasterPlaylist(m) => m,
        Playlist::MediaPlaylist(_) => return Ok(None),
    };

    let best = master
        .variants
        .iter()
        .max_by_key(|v| v.bandwidth)
        .map(|v| -> Result<Variant> {
            Ok(Variant {
                uri: absolutize(&base, &v.uri)?,
                bandwidth: v.bandwidth,
                resolution: v.resolution.map(|r| format!("{}x{}", r.width, r.height)),
            })
        })
        .transpose()?;

    Ok(best)
}

/// Fetch `url` and follow it down to a media playlist, one hop through a master
/// playlist if needed.
///
/// The single-attempt fetch of the rendition playlist is the bug that turned
/// any transient CDN 500 into a dead job; going through `HttpClient` means both
/// hops now get the same retry policy for free, and cannot drift apart again.
pub async fn resolve_media_playlist(
    http: &HttpClient,
    url: &str,
    content_key: Option<[u8; 16]>,
    context: &str,
) -> Result<MediaPlaylist> {
    let body = http.fetch_text(url, context).await?;

    if let Some(variant) = best_variant(&body, url)? {
        tracing::info!(
            "{context}: master playlist, scelta variante {} ({} bps)",
            variant.resolution.as_deref().unwrap_or("risoluzione ignota"),
            variant.bandwidth
        );
        let rendition_context = format!("{context} (rendition)");
        let rendition = http.fetch_text(&variant.uri, &rendition_context).await?;
        return parse_media(&rendition, &variant.uri, content_key);
    }

    parse_media(&body, url, content_key)
}

/// Subtitle renditions advertised by a master playlist, as `(language, uri)`.
///
/// The panel collects these separately through a code path that is prone to
/// 403s, and when it comes back empty the subtitles vanish from the output with
/// no error — the old `M3U8_Downloader` worked around that by re-parsing the
/// master playlist itself. Reading them here, from the playlist the engine has
/// already had to fetch, removes the second code path entirely.
pub fn subtitle_alternatives(body: &str, base_url: &str) -> Result<Vec<(String, String)>> {
    let base = Url::parse(base_url).map_err(|e| EngineError::Playlist {
        context: base_url.to_string(),
        detail: e.to_string(),
    })?;

    let Ok(Playlist::MasterPlaylist(master)) = m3u8_rs::parse_playlist_res(body.as_bytes()) else {
        return Ok(Vec::new());
    };

    let mut out = Vec::new();
    for media in &master.alternatives {
        if media.media_type != m3u8_rs::AlternativeMediaType::Subtitles {
            continue;
        }
        let (Some(language), Some(uri)) = (media.language.as_deref(), media.uri.as_deref()) else {
            continue;
        };
        // vixcloud advertises an "auto" pseudo-language that is not a subtitle
        // anyone asked for.
        if language == "auto" {
            continue;
        }
        out.push((language.to_string(), absolutize(&base, uri)?));
    }
    Ok(out)
}

fn preview(body: &str) -> String {
    let head: String = body.chars().take(200).collect();
    format!("contenuto non riconosciuto: {head:?}")
}

#[cfg(test)]
mod tests {
    use super::*;

    const MASTER: &str = "#EXTM3U\n\
        #EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n\
        360/index.m3u8\n\
        #EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1920x1080\n\
        1080/index.m3u8\n";

    const MEDIA: &str = "#EXTM3U\n\
        #EXT-X-TARGETDURATION:6\n\
        #EXT-X-MEDIA-SEQUENCE:0\n\
        #EXTINF:6.0,\n\
        seg0.ts\n\
        #EXTINF:6.0,\n\
        seg1.ts\n\
        #EXT-X-ENDLIST\n";

    #[test]
    fn picks_the_highest_bandwidth_variant() {
        let best = best_variant(MASTER, "https://cdn.example/v/master.m3u8")
            .expect("parses")
            .expect("has variants");
        assert_eq!(best.uri, "https://cdn.example/v/1080/index.m3u8");
        assert_eq!(best.bandwidth, 4_000_000);
        assert_eq!(best.resolution.as_deref(), Some("1920x1080"));
    }

    #[test]
    fn a_media_playlist_is_not_a_master() {
        assert!(
            best_variant(MEDIA, "https://cdn.example/v/index.m3u8")
                .expect("parses")
                .is_none()
        );
    }

    #[test]
    fn relative_segment_uris_are_absolutized() {
        let media = parse_media(MEDIA, "https://cdn.example/v/index.m3u8", None).expect("parses");
        assert_eq!(
            media.segments,
            vec![
                "https://cdn.example/v/seg0.ts",
                "https://cdn.example/v/seg1.ts"
            ]
        );
    }

    #[test]
    fn an_unencrypted_playlist_ignores_a_supplied_key() {
        let media =
            parse_media(MEDIA, "https://cdn.example/v/index.m3u8", Some([7u8; 16])).expect("parses");
        assert!(matches!(media.encryption, Encryption::None));
    }

    #[test]
    fn an_encrypted_playlist_without_a_key_is_an_error() {
        let encrypted = "#EXTM3U\n\
            #EXT-X-KEY:METHOD=AES-128,URI=\"k.bin\",IV=0x0123456789abcdef0123456789abcdef\n\
            #EXTINF:6.0,\n\
            seg0.ts\n\
            #EXT-X-ENDLIST\n";
        let err = parse_media(encrypted, "https://cdn.example/v/index.m3u8", None)
            .expect_err("must refuse to download what it cannot decrypt");
        assert!(matches!(err, EngineError::Playlist { .. }));
    }

    #[test]
    fn explicit_iv_is_parsed_with_or_without_the_0x_prefix() {
        assert_eq!(
            parse_hex_iv("0x000102030405060708090a0b0c0d0e0f"),
            Some([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15])
        );
        assert_eq!(
            parse_hex_iv("000102030405060708090A0B0C0D0E0F"),
            Some([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15])
        );
        assert_eq!(parse_hex_iv("0xdeadbeef"), None);
    }

    #[test]
    fn subtitle_segments_are_kept_apart_from_video() {
        let subs = "#EXTM3U\n#EXTINF:0,\nsub.vtt\n#EXT-X-ENDLIST\n";
        let parsed = parse_media(subs, "https://cdn.example/s/index.m3u8", None).expect("parses");
        assert!(parsed.segments.is_empty());
        assert_eq!(parsed.subtitles, vec!["https://cdn.example/s/sub.vtt"]);
    }

    #[test]
    fn key_hex_must_be_16_bytes() {
        assert!(parse_key_hex("00112233445566778899aabbccddeeff").is_ok());
        assert!(parse_key_hex("0x00112233445566778899aabbccddeeff").is_ok());
        assert!(parse_key_hex("abc").is_err());
    }
}
