//! Downloading a media playlist's segments to disk.
//!
//! Two invariants carry all the weight here:
//!
//! 1. **A `<n>.ts` on disk is a complete, decrypted segment.** Every segment is
//!    written to `<n>.ts.part`, flushed, and only then renamed into place —
//!    `rename` is atomic within a filesystem. Nothing else may create a `.ts`
//!    in this directory. That single rule is what makes resume safe, and it is
//!    what removed the need for the panel's per-generation temp folders: two
//!    attempts sharing a directory can now only ever agree, because neither can
//!    observe a half-written file.
//!
//! 2. **The join reads by index, never by directory listing.** The old code did
//!    `os.listdir()` and took anything ending in `.ts`, which is how a
//!    leftover `_combined.ts` from an interrupted attempt got muxed in as
//!    though it were segment number 0 (`invalid literal for int()`), and how
//!    missing segments went unnoticed.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use aes::Aes128;
use aes::cipher::block_padding::{NoPadding, Pkcs7};
use aes::cipher::{BlockModeDecrypt, KeyIvInit};
use tokio::io::AsyncWriteExt;
use tokio::sync::Semaphore;
use tokio::task::JoinSet;
use tokio_util::sync::CancellationToken;

use crate::error::{EngineError, IoContext, Result};
use crate::http::HttpClient;
use crate::playlist::{Encryption, MediaPlaylist};
use crate::progress::Progress;

type Aes128CbcDec = cbc::Decryptor<Aes128>;

/// Written into a segment directory the first time this engine touches it.
/// Its absence means the contents came from somewhere that did not promise
/// invariant 1 above (the previous Python downloader, or an interrupted
/// pre-atomic-write build), so they are discarded rather than trusted.
const PROVENANCE_MARKER: &str = ".sc-engine-v1";

/// How many segments may be permanently lost before the download is declared
/// broken. Zero: a video with holes in it is not a successful download, and
/// the user is better served by an error they can act on than by a file that
/// plays for ten minutes and then skips.
const ACCEPTABLE_LOSSES: usize = 0;

pub struct SegmentStore {
    dir: PathBuf,
}

impl SegmentStore {
    /// Open (creating if needed) the directory holding one track's segments.
    pub async fn open(dir: impl Into<PathBuf>) -> Result<Self> {
        let dir = dir.into();
        tokio::fs::create_dir_all(&dir).await.at(&dir)?;

        let marker = dir.join(PROVENANCE_MARKER);
        if !marker.exists() {
            discard_untrusted_contents(&dir).await?;
            tokio::fs::write(&marker, b"1").await.at(&marker)?;
        }

        Ok(Self { dir })
    }

    pub fn segment_path(&self, index: usize) -> PathBuf {
        self.dir.join(format!("{index}.ts"))
    }

    /// Segment indices already on disk, and the bytes they account for.
    ///
    /// Trustworthy by invariant 1 — a `.ts` here cannot be partial.
    async fn existing(&self, count: usize) -> (BTreeSet<usize>, u64) {
        let mut present = BTreeSet::new();
        let mut bytes = 0u64;
        for index in 0..count {
            if let Ok(meta) = tokio::fs::metadata(self.segment_path(index)).await
                && meta.is_file()
                && meta.len() > 0
            {
                present.insert(index);
                bytes += meta.len();
            }
        }
        (present, bytes)
    }
}

/// Remove anything in `dir` that this engine did not write, so the resume scan
/// cannot mistake a foreign or half-written file for a finished segment.
async fn discard_untrusted_contents(dir: &Path) -> Result<()> {
    let mut entries = tokio::fs::read_dir(dir).await.at(dir)?;
    let mut removed = 0usize;
    while let Some(entry) = entries.next_entry().await.at(dir)? {
        let path = entry.path();
        let outcome = match entry.file_type().await {
            Ok(t) if t.is_dir() => tokio::fs::remove_dir_all(&path).await,
            _ => tokio::fs::remove_file(&path).await,
        };
        if outcome.is_ok() {
            removed += 1;
        }
    }
    if removed > 0 {
        tracing::info!(
            "{}: scartati {removed} file di provenienza ignota (ripartono da capo)",
            dir.display()
        );
    }
    Ok(())
}

/// Decrypt one segment in place, per the HLS AES-128 rules.
fn decrypt(data: Vec<u8>, encryption: &Encryption, sequence: u64) -> Result<Vec<u8>> {
    let (key, iv) = match encryption {
        Encryption::None => return Ok(data),
        Encryption::Aes128 { key, iv } => (key, iv),
    };

    // An absent IV means "use the segment's media sequence number", 128-bit
    // big-endian. The Python version instead skipped decryption entirely in
    // this case, writing ciphertext to disk as if it were video.
    let iv = iv.unwrap_or_else(|| (sequence as u128).to_be_bytes());

    if data.is_empty() || !data.len().is_multiple_of(16) {
        // Not a valid CBC ciphertext: the body was truncated in transit. Report
        // it as a failed segment so the retry path gets a chance, instead of
        // writing garbage that only surfaces as an unplayable file.
        return Err(EngineError::BadKey(format!(
            "segmento cifrato di {} byte, non multiplo di 16 (download troncato)",
            data.len()
        )));
    }

    let mut buf = data;
    let decryptor = Aes128CbcDec::new(key.into(), &iv.into());
    // Most HLS packagers PKCS7-pad the segment; a few emit block-aligned
    // payloads with no padding at all. Try the spec-conformant reading first
    // and fall back rather than either rejecting valid streams or leaving
    // padding bytes in the TS stream (which is what the old code did).
    match decryptor.decrypt_padded::<Pkcs7>(&mut buf) {
        Ok(plain) => {
            let len = plain.len();
            buf.truncate(len);
            Ok(buf)
        }
        Err(_) => {
            let decryptor = Aes128CbcDec::new(key.into(), &iv.into());
            decryptor
                .decrypt_padded::<NoPadding>(&mut buf)
                .map_err(|e| EngineError::BadKey(format!("decifratura fallita: {e}")))?;
            Ok(buf)
        }
    }
}

/// Fetch, decrypt and atomically place one segment. Returns bytes written.
async fn fetch_one(
    http: &HttpClient,
    store: &SegmentStore,
    url: &str,
    index: usize,
    sequence: u64,
    encryption: &Encryption,
) -> Result<u64> {
    let context = format!("segmento {index}");
    let raw = http.fetch_segment(url, &context).await?;
    let plain = decrypt(raw, encryption, sequence)?;

    let final_path = store.segment_path(index);
    let part_path = final_path.with_extension("ts.part");

    let mut file = tokio::fs::File::create(&part_path).await.at(&part_path)?;
    file.write_all(&plain).await.at(&part_path)?;
    // Flush to the device before the rename: without it a crash can leave a
    // visible but empty `.ts`, and invariant 1 would be a lie exactly when it
    // matters most.
    file.sync_all().await.at(&part_path)?;
    drop(file);

    tokio::fs::rename(&part_path, &final_path)
        .await
        .at(&final_path)?;

    Ok(plain.len() as u64)
}

/// Download every segment of `playlist` into `store`.
///
/// Returns the ordered list of segment files, ready for the join. Any segment
/// still missing after both passes is an error — never a shorter list.
pub async fn download_all(
    http: &HttpClient,
    playlist: &MediaPlaylist,
    store: &SegmentStore,
    workers: usize,
    cancel: &CancellationToken,
    progress: &Progress,
) -> Result<Vec<PathBuf>> {
    let total = playlist.segments.len();
    let (present, resumed_bytes) = store.existing(total).await;

    if !present.is_empty() {
        tracing::info!(
            "{} segmenti su {total} già presenti, si riprende da lì",
            present.len()
        );
        progress.advance(present.len() as u64, resumed_bytes);
    }

    let pending: Vec<usize> = (0..total).filter(|i| !present.contains(i)).collect();

    // ── Pass 1: parallel ───────────────────────────────────────────────────
    let semaphore = Arc::new(Semaphore::new(workers.max(1)));
    let mut tasks: JoinSet<(usize, Result<u64>)> = JoinSet::new();

    for index in pending {
        let http = http.clone();
        let url = playlist.segments[index].clone();
        let encryption = playlist.encryption.clone();
        let sequence = playlist.media_sequence + index as u64;
        let dir = store.dir.clone();
        let semaphore = Arc::clone(&semaphore);
        let cancel = cancel.clone();

        tasks.spawn(async move {
            if cancel.is_cancelled() {
                return (index, Err(EngineError::Cancelled));
            }
            // Concurrency is bounded here rather than at spawn time so the
            // whole set can be aborted instantly on cancellation.
            let Ok(_permit) = semaphore.acquire().await else {
                return (index, Err(EngineError::Cancelled));
            };
            let store = SegmentStore { dir };
            let outcome = fetch_one(&http, &store, &url, index, sequence, &encryption).await;
            (index, outcome)
        });
    }

    let mut failed: BTreeSet<usize> = BTreeSet::new();

    loop {
        let joined = tokio::select! {
            biased;
            _ = cancel.cancelled() => {
                tasks.shutdown().await;
                return Err(EngineError::Cancelled);
            }
            joined = tasks.join_next() => joined,
        };

        let Some(joined) = joined else { break };

        match joined {
            Ok((_index, Ok(bytes))) => progress.advance(1, bytes),
            Ok((_index, Err(EngineError::Cancelled))) => {
                tasks.shutdown().await;
                return Err(EngineError::Cancelled);
            }
            Ok((index, Err(e))) => {
                tracing::warn!("segmento {index} fallito: {e}");
                failed.insert(index);
            }
            Err(join_error) if join_error.is_cancelled() => {}
            Err(join_error) => {
                // A worker panicked. Nothing is corrupted (the segment simply
                // was not renamed into place), but it must not be silently
                // counted as done.
                tracing::error!("worker terminato in modo anomalo: {join_error}");
                return Err(EngineError::Io {
                    path: store.dir.clone(),
                    source: std::io::Error::other(join_error.to_string()),
                });
            }
        }
    }

    // ── Pass 2: sequential, for whatever the CDN refused under load ────────
    if !failed.is_empty() {
        tracing::warn!(
            "{} segmenti da ritentare in sequenza",
            failed.len()
        );
        let retry_list: Vec<usize> = failed.iter().copied().collect();
        for index in retry_list {
            if cancel.is_cancelled() {
                return Err(EngineError::Cancelled);
            }
            let sequence = playlist.media_sequence + index as u64;
            match fetch_one(
                http,
                store,
                &playlist.segments[index],
                index,
                sequence,
                &playlist.encryption,
            )
            .await
            {
                Ok(bytes) => {
                    tracing::info!("segmento {index} recuperato al secondo passaggio");
                    failed.remove(&index);
                    progress.advance(1, bytes);
                }
                Err(EngineError::Cancelled) => return Err(EngineError::Cancelled),
                Err(e) => tracing::error!("segmento {index} definitivamente perso: {e}"),
            }
        }
    }

    if failed.len() > ACCEPTABLE_LOSSES {
        return Err(EngineError::MissingSegments {
            count: failed.len(),
            preview: preview_indices(&failed),
        });
    }

    // Built from the playlist, so a gap is impossible by construction.
    let mut files = Vec::with_capacity(total);
    for index in 0..total {
        let path = store.segment_path(index);
        if !path.exists() {
            // Belt and braces: the accounting above says this cannot happen,
            // so if it does, say so instead of muxing a short file.
            return Err(EngineError::MissingSegments {
                count: 1,
                preview: format!("[{index}]"),
            });
        }
        files.push(path);
    }
    Ok(files)
}

fn preview_indices(indices: &BTreeSet<usize>) -> String {
    let head: Vec<String> = indices.iter().take(10).map(|i| i.to_string()).collect();
    let suffix = if indices.len() > 10 { ", ..." } else { "" };
    format!("[{}{}]", head.join(", "), suffix)
}

#[cfg(test)]
mod tests {
    use super::*;
    use aes::cipher::BlockModeEncrypt;

    type Aes128CbcEnc = cbc::Encryptor<Aes128>;

    const KEY: [u8; 16] = [
        0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88, 0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee,
        0xff,
    ];

    fn encrypt_pkcs7(plain: &[u8], iv: [u8; 16]) -> Vec<u8> {
        let mut buf = vec![0u8; plain.len() + 16];
        let encryptor = Aes128CbcEnc::new(&KEY.into(), &iv.into());
        let len = encryptor
            .encrypt_padded_b2b::<Pkcs7>(plain, &mut buf)
            .expect("encrypts")
            .len();
        buf.truncate(len);
        buf
    }

    #[test]
    fn round_trips_with_an_explicit_iv() {
        let iv = [9u8; 16];
        let plain = b"a TS packet, more or less".to_vec();
        let cipher = encrypt_pkcs7(&plain, iv);
        let encryption = Encryption::Aes128 {
            key: KEY,
            iv: Some(iv),
        };
        assert_eq!(decrypt(cipher, &encryption, 0).expect("decrypts"), plain);
    }

    /// The case the Python version got wrong: no IV attribute means "use the
    /// sequence number", not "do not decrypt".
    #[test]
    fn round_trips_with_an_implicit_sequence_iv() {
        let sequence = 42u64;
        let iv = (sequence as u128).to_be_bytes();
        let plain = b"segment forty-two".to_vec();
        let cipher = encrypt_pkcs7(&plain, iv);
        let encryption = Encryption::Aes128 { key: KEY, iv: None };
        assert_eq!(
            decrypt(cipher, &encryption, sequence).expect("decrypts"),
            plain
        );
    }

    #[test]
    fn the_wrong_sequence_number_does_not_silently_yield_plausible_output() {
        let iv = (7u128).to_be_bytes();
        let plain = vec![0xABu8; 64];
        let cipher = encrypt_pkcs7(&plain, iv);
        let encryption = Encryption::Aes128 { key: KEY, iv: None };
        // Decrypting with the wrong IV corrupts the first block; the rest still
        // decrypts, so the guarantee is only that it differs from the original.
        if let Ok(out) = decrypt(cipher, &encryption, 8) { assert_ne!(out, plain) }
    }

    #[test]
    fn a_truncated_ciphertext_is_rejected_rather_than_written() {
        let encryption = Encryption::Aes128 {
            key: KEY,
            iv: Some([0u8; 16]),
        };
        let err = decrypt(vec![1, 2, 3], &encryption, 0)
            .expect_err("a non-block-aligned body is a truncated download");
        assert!(matches!(err, EngineError::BadKey(_)));
    }

    #[test]
    fn unencrypted_data_passes_through_untouched() {
        let data = b"plain TS".to_vec();
        assert_eq!(
            decrypt(data.clone(), &Encryption::None, 0).expect("passes through"),
            data
        );
    }

    #[tokio::test]
    async fn opening_a_foreign_directory_discards_its_contents() {
        let dir = std::env::temp_dir().join(format!("sc-engine-test-{}", std::process::id()));
        let _ = tokio::fs::remove_dir_all(&dir).await;
        tokio::fs::create_dir_all(&dir).await.expect("mkdir");
        // Exactly the situation that crashed a resume: a stray combined file
        // and a segment of unknown completeness.
        tokio::fs::write(dir.join("_combined.ts"), b"junk")
            .await
            .expect("write");
        tokio::fs::write(dir.join("0.ts"), b"half a segment")
            .await
            .expect("write");

        let store = SegmentStore::open(&dir).await.expect("opens");
        let (present, bytes) = store.existing(4).await;
        assert!(present.is_empty(), "foreign segments must not be trusted");
        assert_eq!(bytes, 0);
        assert!(!dir.join("_combined.ts").exists());

        // Second open keeps what the engine itself wrote.
        tokio::fs::write(store.segment_path(0), b"engine-written")
            .await
            .expect("write");
        let store = SegmentStore::open(&dir).await.expect("reopens");
        let (present, bytes) = store.existing(4).await;
        assert_eq!(present.into_iter().collect::<Vec<_>>(), vec![0]);
        assert_eq!(bytes, 14);

        let _ = tokio::fs::remove_dir_all(&dir).await;
    }

    #[test]
    fn missing_indices_are_previewed_compactly() {
        let many: BTreeSet<usize> = (0..25).collect();
        let preview = preview_indices(&many);
        assert!(preview.starts_with("[0, 1, 2"));
        assert!(preview.ends_with(", ...]"));
    }
}
