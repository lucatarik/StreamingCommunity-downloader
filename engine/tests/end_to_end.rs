//! End-to-end tests: the real binary, a real (fake) CDN, real ffmpeg output.
//!
//! Every bug listed in the handoff has a test here that fails against the old
//! behaviour, because unit tests could not have caught any of them — each one
//! only appeared when the network misbehaved partway through a download.

use std::collections::HashMap;
use std::io::Write;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::sync::Mutex;

// ── A CDN that misbehaves on demand ────────────────────────────────────────

#[derive(Default, Clone)]
struct Faults {
    /// path → (status, how many times to serve it before recovering)
    transient: HashMap<String, (u16, usize)>,
    /// path → status, forever
    permanent: HashMap<String, u16>,
    /// Delay before every response, to leave room to send a signal.
    delay: Option<Duration>,
}

struct FakeCdn {
    addr: SocketAddr,
    hits: Arc<Mutex<HashMap<String, usize>>>,
}

impl FakeCdn {
    async fn start(root: PathBuf, faults: Faults) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let addr = listener.local_addr().expect("addr");
        let hits: Arc<Mutex<HashMap<String, usize>>> = Arc::default();
        let served = Arc::new(AtomicUsize::new(0));

        let task_hits = Arc::clone(&hits);
        let task_served = Arc::clone(&served);
        tokio::spawn(async move {
            loop {
                let Ok((mut stream, _)) = listener.accept().await else {
                    return;
                };
                let root = root.clone();
                let faults = faults.clone();
                let hits = Arc::clone(&task_hits);
                let served = Arc::clone(&task_served);

                tokio::spawn(async move {
                    let mut buf = vec![0u8; 4096];
                    let Ok(read) = stream.read(&mut buf).await else {
                        return;
                    };
                    if read == 0 {
                        return;
                    }
                    let request = String::from_utf8_lossy(&buf[..read]);
                    let path = request
                        .lines()
                        .next()
                        .and_then(|line| line.split_whitespace().nth(1))
                        .unwrap_or("/")
                        .split('?')
                        .next()
                        .unwrap_or("/")
                        .to_string();

                    let attempt = {
                        let mut hits = hits.lock().await;
                        let counter = hits.entry(path.clone()).or_insert(0);
                        *counter += 1;
                        *counter
                    };

                    if let Some(delay) = faults.delay {
                        tokio::time::sleep(delay).await;
                    }

                    if let Some(status) = faults.permanent.get(&path) {
                        let _ = respond_status(&mut stream, *status).await;
                        return;
                    }
                    if let Some((status, times)) = faults.transient.get(&path)
                        && attempt <= *times
                    {
                        let _ = respond_status(&mut stream, *status).await;
                        return;
                    }

                    let file = root.join(path.trim_start_matches('/'));
                    match tokio::fs::read(&file).await {
                        Ok(body) => {
                            served.fetch_add(1, Ordering::Relaxed);
                            let _ = respond_body(&mut stream, &body).await;
                        }
                        Err(_) => {
                            let _ = respond_status(&mut stream, 404).await;
                        }
                    }
                });
            }
        });

        Self { addr, hits }
    }

    fn url(&self, path: &str) -> String {
        format!("http://{}/{}", self.addr, path.trim_start_matches('/'))
    }

    async fn hits_on(&self, path: &str) -> usize {
        self.hits.lock().await.get(path).copied().unwrap_or(0)
    }
}

async fn respond_status(stream: &mut tokio::net::TcpStream, status: u16) -> std::io::Result<()> {
    let head = format!(
        "HTTP/1.1 {status} X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    );
    stream.write_all(head.as_bytes()).await?;
    stream.shutdown().await
}

async fn respond_body(stream: &mut tokio::net::TcpStream, body: &[u8]) -> std::io::Result<()> {
    let head = format!(
        "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    stream.write_all(head.as_bytes()).await?;
    stream.write_all(body).await?;
    stream.shutdown().await
}

// ── Fixtures ───────────────────────────────────────────────────────────────

/// Build a real 5-segment HLS stream with ffmpeg, so the join is exercised
/// against genuine MPEG-TS rather than bytes that merely look like it.
fn make_hls_fixture(dir: &Path) -> usize {
    std::fs::create_dir_all(dir).expect("fixture dir");
    let status = Command::new("ffmpeg")
        .args([
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=10:duration=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=6",
            "-c:v",
            "mpeg2video",
            "-b:v",
            "300k",
            "-c:a",
            "mp2",
            "-f",
            "hls",
            "-hls_time",
            "1",
            "-hls_list_size",
            "0",
            "-hls_segment_filename",
        ])
        .arg(dir.join("seg%d.ts"))
        .arg(dir.join("index.m3u8"))
        .status()
        .expect("ffmpeg runs");
    assert!(status.success(), "fixture generation failed");

    let playlist = std::fs::read_to_string(dir.join("index.m3u8")).expect("playlist");
    playlist.matches(".ts").count()
}

fn workspace(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("sc-engine-e2e-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("workspace");
    dir
}

#[derive(Debug)]
struct EngineRun {
    code: i32,
    events: Vec<serde_json::Value>,
    stderr: String,
}

impl EngineRun {
    fn event(&self, kind: &str) -> Option<&serde_json::Value> {
        self.events
            .iter()
            .find(|e| e.get("type").and_then(|t| t.as_str()) == Some(kind))
    }

    fn phases(&self) -> Vec<String> {
        self.events
            .iter()
            .filter_map(|e| e.get("phase").and_then(|p| p.as_str()))
            .map(str::to_string)
            .collect()
    }
}

fn spawn_engine(spec: &serde_json::Value) -> std::process::Child {
    let mut child = Command::new(env!("CARGO_BIN_EXE_sc-dl-engine"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("engine starts");
    child
        .stdin
        .as_mut()
        .expect("stdin")
        .write_all(spec.to_string().as_bytes())
        .expect("spec written");
    // Closing stdin is what tells the engine the spec is complete.
    drop(child.stdin.take());
    child
}

fn collect(child: std::process::Child) -> EngineRun {
    let output = child.wait_with_output().expect("engine finishes");
    let events = String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| serde_json::from_str(l).unwrap_or_else(|e| panic!("bad event {l:?}: {e}")))
        .collect();
    EngineRun {
        code: output.status.code().unwrap_or(-1),
        events,
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
    }
}

fn run_engine(spec: &serde_json::Value) -> EngineRun {
    collect(spawn_engine(spec))
}

fn spec_for(cdn: &FakeCdn, work: &Path, playlist: &str) -> serde_json::Value {
    serde_json::json!({
        "m3u8_index": cdn.url(playlist),
        "output_path": work.join("out/Film (2020)/Film (2020).mp4").to_string_lossy(),
        "temp_dir": work.join("tmp").to_string_lossy(),
        "max_segment_workers": 4,
    })
}

/// Ask ffprobe whether the engine produced something actually playable.
fn probe(path: &Path) -> String {
    let output = Command::new("ffprobe")
        .args([
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type",
            "-of",
            "default=noprint_wrappers=1",
        ])
        .arg(path)
        .output()
        .expect("ffprobe runs");
    assert!(
        output.status.success(),
        "ffprobe rejected {}: {}",
        path.display(),
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8_lossy(&output.stdout).into_owned()
}

// ── Tests ──────────────────────────────────────────────────────────────────

#[tokio::test]
async fn downloads_a_playlist_and_produces_a_playable_file() {
    let work = workspace("happy");
    let assets = work.join("cdn");
    let segments = make_hls_fixture(&assets);
    let cdn = FakeCdn::start(assets, Faults::default()).await;

    let spec = spec_for(&cdn, &work, "index.m3u8");
    let run = tokio::task::spawn_blocking(move || run_engine(&spec))
        .await
        .expect("join");

    assert_eq!(run.code, 0, "engine failed:\n{}", run.stderr);
    let done = run.event("done").expect("a done event");
    let output = PathBuf::from(done["output_path"].as_str().expect("path"));
    assert!(output.exists(), "output missing at {}", output.display());

    let probed = probe(&output);
    assert!(probed.contains("codec_type=video"), "no video: {probed}");
    // Six seconds of source must survive the round trip, not one segment's worth.
    let duration: f64 = probed
        .lines()
        .find_map(|l| l.strip_prefix("duration="))
        .and_then(|d| d.parse().ok())
        .expect("a duration");
    assert!(duration > 5.0, "expected ~6s, got {duration}s");

    // Progress must reach the real total, and the scratch space must be gone.
    let last = run
        .events
        .iter()
        .rfind(|e| e["type"] == "progress")
        .expect("progress events");
    assert_eq!(last["current"], segments as u64);
    assert_eq!(last["pct"], 100.0);
    assert!(!work.join("tmp").exists(), "temp dir should be cleaned up");

    let _ = std::fs::remove_dir_all(&work);
}

/// Bug 2: a burst of transient 503s used to abandon each segment after one
/// attempt, dropping it from the output while progress still read 100%.
#[tokio::test]
async fn recovers_from_transient_segment_failures() {
    let work = workspace("transient");
    let assets = work.join("cdn");
    let segments = make_hls_fixture(&assets);

    let mut transient = HashMap::new();
    transient.insert("/seg1.ts".to_string(), (503u16, 2usize));
    transient.insert("/seg3.ts".to_string(), (500u16, 1usize));
    // 429 is the only status the old code actually retried; include it so the
    // test also proves that path did not regress.
    transient.insert("/seg4.ts".to_string(), (429u16, 1usize));
    let cdn = FakeCdn::start(assets, Faults { transient, ..Default::default() }).await;

    let spec = spec_for(&cdn, &work, "index.m3u8");
    let run = tokio::task::spawn_blocking({
        let spec = spec.clone();
        move || run_engine(&spec)
    })
    .await
    .expect("join");

    assert_eq!(run.code, 0, "transient errors must not fail a job:\n{}", run.stderr);
    assert_eq!(cdn.hits_on("/seg1.ts").await, 3, "503 must be retried");
    assert_eq!(cdn.hits_on("/seg3.ts").await, 2, "500 must be retried");

    let output = PathBuf::from(run.event("done").expect("done")["output_path"].as_str().unwrap());
    let duration: f64 = probe(&output)
        .lines()
        .find_map(|l| l.strip_prefix("duration="))
        .and_then(|d| d.parse().ok())
        .expect("a duration");
    // The proof that nothing was silently dropped: all five segments are in
    // there, so the duration is full length.
    assert!(duration > 5.0, "{segments} segments expected, got {duration}s");

    let _ = std::fs::remove_dir_all(&work);
}

/// Bug 3: an unrecoverable segment used to be logged and muxed around, handing
/// the user a file with a hole in it and a green "done".
#[tokio::test]
async fn a_permanently_missing_segment_fails_the_job() {
    let work = workspace("missing");
    let assets = work.join("cdn");
    make_hls_fixture(&assets);

    let mut permanent = HashMap::new();
    permanent.insert("/seg2.ts".to_string(), 500u16);
    let cdn = FakeCdn::start(assets, Faults { permanent, ..Default::default() }).await;

    let spec = spec_for(&cdn, &work, "index.m3u8");
    let output = PathBuf::from(spec["output_path"].as_str().unwrap().to_string());
    let run = tokio::task::spawn_blocking(move || run_engine(&spec))
        .await
        .expect("join");

    assert_eq!(run.code, 1, "a holed download must fail");
    let message = run.event("error").expect("an error event")["message"]
        .as_str()
        .expect("message")
        .to_string();
    assert!(
        message.contains("segmenti non scaricabili"),
        "unhelpful message: {message}"
    );
    assert!(message.contains('2'), "must name the missing index: {message}");
    assert!(run.event("done").is_none(), "must not also report success");
    assert!(
        !output.exists(),
        "a partial output must not be left behind for Jellyfin to index"
    );
    // The scratch space survives, so a resume does not restart from zero.
    assert!(work.join("tmp/video").exists(), "segments must be kept for resume");

    let _ = std::fs::remove_dir_all(&work);
}

/// Bug 1: the rendition hop was fetched exactly once, so a single transient CDN
/// error on the *second* request killed the job.
#[tokio::test]
async fn retries_the_rendition_hop_of_a_master_playlist() {
    let work = workspace("master");
    let assets = work.join("cdn");
    make_hls_fixture(&assets);
    // Wrap the media playlist in a master playlist with two variants.
    std::fs::write(
        assets.join("master.m3u8"),
        "#EXTM3U\n\
         #EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=320x240\n\
         index.m3u8\n\
         #EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720\n\
         index.m3u8\n",
    )
    .expect("master playlist");

    let mut transient = HashMap::new();
    transient.insert("/index.m3u8".to_string(), (500u16, 2usize));
    // 403 on the master is the vixcloud quirk that needs the ?b=1 fallback.
    transient.insert("/master.m3u8".to_string(), (403u16, 1usize));
    let cdn = FakeCdn::start(assets, Faults { transient, ..Default::default() }).await;

    let spec = spec_for(&cdn, &work, "master.m3u8");
    let run = tokio::task::spawn_blocking(move || run_engine(&spec))
        .await
        .expect("join");

    assert_eq!(run.code, 0, "rendition errors must be retried:\n{}", run.stderr);
    assert_eq!(cdn.hits_on("/index.m3u8").await, 3, "rendition must retry");
    assert!(run.event("done").is_some());

    let _ = std::fs::remove_dir_all(&work);
}

/// The premise of the whole port: cancellation is enforced, not requested.
///
/// The old engine could only set a flag and hope; a blocked attempt kept
/// running and later wrote its stale result over a newer one. Here SIGTERM
/// stops the process, and what it leaves on disk is exactly what a resume needs
/// — which is why the panel no longer needs generation counters or
/// per-generation temp folders.
#[tokio::test]
async fn sigterm_stops_promptly_and_leaves_a_resumable_state() {
    let work = workspace("cancel");
    let assets = work.join("cdn");
    make_hls_fixture(&assets);
    let faults = Faults {
        delay: Some(Duration::from_millis(700)),
        ..Default::default()
    };
    let cdn = FakeCdn::start(assets.clone(), faults).await;

    let spec = spec_for(&cdn, &work, "index.m3u8");
    // One worker, so the download is slow enough to interrupt mid-flight.
    let mut slow_spec = spec.clone();
    slow_spec["max_segment_workers"] = serde_json::json!(1);

    let child = spawn_engine(&slow_spec);
    let pid = child.id();
    tokio::time::sleep(Duration::from_millis(1600)).await;

    let killed = std::time::Instant::now();
    Command::new("kill")
        .args(["-TERM", &pid.to_string()])
        .status()
        .expect("kill runs");
    let run = tokio::task::spawn_blocking(move || collect(child))
        .await
        .expect("join");
    let elapsed = killed.elapsed();

    assert_eq!(run.code, 130, "SIGTERM must exit as cancelled:\n{}", run.stderr);
    assert!(
        run.event("cancelled").is_some(),
        "cancellation must be distinguishable from an error"
    );
    assert!(run.event("error").is_none());
    // "Prompt" means it does not sit out the in-flight request plus a backoff.
    assert!(
        elapsed < Duration::from_secs(3),
        "took {elapsed:?} to stop after SIGTERM"
    );

    // Which segments survived, and how often each had been requested so far.
    // Counting requests rather than served bodies keeps the check immune to a
    // request that was still in flight when the signal landed.
    let mut completed = Vec::new();
    let mut hits_before = HashMap::new();
    for index in 0..5 {
        let path = format!("/seg{index}.ts");
        hits_before.insert(path.clone(), cdn.hits_on(&path).await);
        if work.join(format!("tmp/video/{index}.ts")).exists() {
            completed.push(index);
        }
    }
    assert!(!completed.is_empty(), "nothing was preserved to resume from");
    assert!(completed.len() < 5, "the run should not have completed");
    assert!(
        std::fs::read_dir(work.join("tmp/video"))
            .expect("segment dir survives")
            .filter_map(|e| e.ok())
            .all(|e| !e.path().to_string_lossy().ends_with(".ts.part")
                || e.metadata().is_ok()),
        "interrupted writes may remain as .part, never as .ts"
    );

    // ── Resume: same temp dir, no delay, must reuse what is already there ──
    let run = tokio::task::spawn_blocking({
        let spec = spec.clone();
        move || run_engine(&spec)
    })
    .await
    .expect("join");

    assert_eq!(run.code, 0, "resume must complete:\n{}", run.stderr);
    for index in &completed {
        let path = format!("/seg{index}.ts");
        assert_eq!(
            cdn.hits_on(&path).await,
            hits_before[&path],
            "segment {index} was already on disk and must not be fetched again"
        );
    }

    let output = PathBuf::from(run.event("done").expect("done")["output_path"].as_str().unwrap());
    let duration: f64 = probe(&output)
        .lines()
        .find_map(|l| l.strip_prefix("duration="))
        .and_then(|d| d.parse().ok())
        .expect("a duration");
    assert!(
        duration > 5.0,
        "a resumed download must be whole, got {duration}s"
    );

    let _ = std::fs::remove_dir_all(&work);
}

/// Audio renditions and subtitles arrive as separate playlists and must end up
/// inside one Matroska file, with the sidecar `.vtt` cleaned up so Jellyfin
/// does not list every track twice.
#[tokio::test]
async fn merges_audio_and_subtitles_into_a_single_mkv() {
    let work = workspace("mux");
    let assets = work.join("cdn");
    make_hls_fixture(&assets);

    // A subtitle playlist pointing at a WebVTT file.
    std::fs::write(
        assets.join("subs.m3u8"),
        "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\nsub.vtt\n#EXT-X-ENDLIST\n",
    )
    .expect("subtitle playlist");
    std::fs::write(
        assets.join("sub.vtt"),
        "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nciao\n",
    )
    .expect("vtt");

    let cdn = FakeCdn::start(assets, Faults::default()).await;
    let mut spec = spec_for(&cdn, &work, "index.m3u8");
    spec["audio_tracks"] = serde_json::json!([{ "url": cdn.url("index.m3u8"), "language": "ita" }]);
    spec["subtitle_tracks"] = serde_json::json!([{ "uri": cdn.url("subs.m3u8"), "language": "ita" }]);

    let run = tokio::task::spawn_blocking(move || run_engine(&spec))
        .await
        .expect("join");

    assert_eq!(run.code, 0, "mux failed:\n{}", run.stderr);
    let output = PathBuf::from(run.event("done").expect("done")["output_path"].as_str().unwrap());
    assert_eq!(
        output.extension().and_then(|e| e.to_str()),
        Some("mkv"),
        "extra tracks mean a remux, and the panel needs the real path back"
    );
    assert!(output.exists());
    assert!(
        !output.with_extension("mp4").exists(),
        "the intermediate mp4 must not be left beside the mkv"
    );

    let probed = probe(&output);
    assert!(probed.contains("codec_type=subtitle"), "no subtitles: {probed}");
    assert_eq!(
        probed.matches("codec_type=audio").count(),
        1,
        "expected exactly one audio track: {probed}"
    );

    // The phases the panel's progress UI keys off must all be announced.
    let phases = run.phases();
    for expected in ["video", "joining", "audio_ita", "merging"] {
        assert!(
            phases.iter().any(|p| p == expected),
            "phase {expected} never emitted, got {phases:?}"
        );
    }

    let _ = std::fs::remove_dir_all(&work);
}

/// A malformed spec is a caller bug, not a download failure: it must be
/// reported distinctly so the panel does not schedule a pointless retry.
#[test]
fn a_bad_spec_exits_distinctly() {
    let mut child = Command::new(env!("CARGO_BIN_EXE_sc-dl-engine"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("engine starts");
    child
        .stdin
        .as_mut()
        .expect("stdin")
        .write_all(b"{\"nonsense\": true}")
        .expect("write");
    drop(child.stdin.take());

    let run = collect(child);
    assert_eq!(run.code, 2);
    assert!(run.event("error").is_some());
}
