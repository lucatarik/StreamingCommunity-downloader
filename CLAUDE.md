# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A self-hosted FastAPI web panel that searches and downloads films, TV series and anime from
StreamingCommunity and AnimeUnity into a Jellyfin library. It handles M3U8 parsing, AES-CBC segment
decryption, parallel downloading and FFmpeg merging.

Access can be authenticated against a Jellyfin server; there is no local password store.
Authentication is **opt-in**: `AUTH_ENABLED` defaults to `0`, which runs the panel open (no login),
so pulling a newer image never locks out a deployment whose compose file predates the variable.
The shipped compose sets `AUTH_ENABLED=1`.

## Running the Project

```bash
pip install -r requirements.txt
python main.py            # http://127.0.0.1:8000, open mode

pip install -r requirements-dev.txt   # tests only; kept out of the runtime image
pytest -q
```

**Prerequisites:** FFmpeg on PATH, and the download engine built (the Docker
image does both). `app/core/engine.py` looks for the binary in `SC_ENGINE_BIN`,
then on PATH, then in `engine/target/{release,debug}/`.

**Single process only.** Download job state lives in memory in `JobManager`, so running uvicorn with
`--workers > 1` or scaling the container would leave each worker blind to the others' downloads.

## Architecture

### Entry flow

`main.py` → `app.main:app`. The lifespan runs database migrations, registers the request→job
listener and starts the schedule loop. `AuthMiddleware` resolves the session cookie for every
request before any route runs.

### Layout

**`engine/`** — the download engine, in Rust (`sc-dl-engine`)

Everything below the resolved M3U8 URL: retries, AES-128 decryption, segment
storage, resume, ffmpeg muxing. It runs as a **child process**, reads one JSON
job spec on stdin and reports newline-delimited events on stdout. See
`engine/src/main.rs` for the reasoning, and `engine/README.md` for the protocol.

It is a separate process on purpose. A blocking network call cannot be killed
from outside its thread in Python, so a cancel could only be *requested*; a
wedged attempt kept running and later wrote stale state over a newer one. A
child process can be killed, so cancellation is enforceable — which is what
allows `jobs.py` to assume one live attempt per job.

```bash
cargo test --manifest-path engine/Cargo.toml       # 35 unit + 7 end-to-end
cargo build --release --manifest-path engine/Cargo.toml
```

The end-to-end tests run the real binary against a fake CDN that injects 503s,
403s and stalls, and check the output with ffprobe. They need `ffmpeg` and
`ffprobe` on PATH.

**`app/core/`** — source interaction, and the bridge to the engine
- `page.py` — domain check, search
- `film.py` — movie resolve + download; also owns `_collect_audio_tracks` / `_collect_subtitle_tracks`, which `tv.py` and `animeunity.py` import
- `tv.py` — seasons, episodes, per-episode languages, episode download
- `animeunity.py` — AnimeUnity search, episodes, download
- `engine.py` — spawns `sc-dl-engine`, forwards its events to the progress bars, and turns `cancel_event` into SIGTERM (escalating to SIGKILL)
- `m3u8.py` — `M3U8_Parser` (still used for language discovery) and `download_m3u8()`, which now delegates to the engine
- `paths.py` — destination paths (`film_path`, `episode_path`, `anime_path`). The request system's library check goes through these, so a change here changes both.
- `headers.py` — user-agent rotation, `sanitize_filename`
- `_shared.py` — embed parsing, M3U8 URL/key, `MissingAudioTrackError`

**`app/auth/`** — Jellyfin SSO, permissions, users
- `jellyfin.py` — HTTP client (MediaBrowser header, stable DeviceId, X-Forwarded-For with retry)
- `session.py` — server-side sessions (opaque token, SHA-256 stored)
- `permissions.py` — independent `IntFlag` permissions
- `deps.py` — `AuthMiddleware`, public allowlist, `require(...)`
- `router.py` / `users_router.py` — setup, login, user import and management

**`app/requests/`** — the request queue
- `models.py` — records, content key, allowed transitions
- `resolver.py` — re-resolution at approval, track verification, library check
- `service.py` — lifecycle and job wiring
- `notify.py` — in-app notifications
- `router.py` — endpoints

**`app/`** — `jobs.py` (thread pool, semaphore, SSE broadcast), `schedule.py`, `db.py`, `config.py`,
`progress.py`, `routers/`, `templates/`, `static/`

### Persistence

- `panel.db` (SQLite, stdlib `sqlite3`) — users, sessions, requests, notifications. Migrations are
  the ordered `MIGRATIONS` list in `app/db.py`, applied against `PRAGMA user_version`. Never edit an
  applied migration; append a new one.
- `data.json` — source domain, library paths, performance settings. Runtime state, not committed:
  a baked-in source domain ships stale, since the domain rotates. Tests get one from the
  `_configured_domain` autouse fixture in `tests/conftest.py`.
- `schedule.json` — scheduled downloads

Point `DB_FILE`, `DATA_FILE` and `SCHEDULE_FILE` at a persistent volume in Docker; see
`docker-compose.template.yml`.

`docs/` holds README screenshots only. It is **not** served by the app and is excluded from the
image — the favicon lives in `app/static/`. Design notes go in `design/`, likewise never served:
anything under a mounted static directory is readable by unauthenticated visitors.

### Rules that are easy to break

- **The source domain never comes from the client.** Use `app.config.configured_domain()`. Same for
  the requester's identity, which comes from the session, never from a request body.
- **`AUTH_ENABLED` is read as an import-time constant** in `app/auth/deps.py`, `app/auth/router.py`
  and `app/main.py`. Patching `app.config` alone does not reach those bindings — all three must be
  patched (see the `_auth_enabled` fixture in `tests/conftest.py`). Open mode is also reachable at
  runtime via `models.runtime_open_mode()` (the `auth_mode` setting), so both paths must be checked
  wherever one is.
- **Every `/api` route needs a decision**: public allowlist, `SESSION_ONLY_PATHS`, or a
  `require(...)` dependency. `tests/test_permissions.py` fails otherwise.
- **No ADMIN super-permission.** Flags are independent, so an administrator can exist who never sees
  the request queue.
- **Never substitute an audio track.** `strict_audio=True` on the request path turns a missing
  language into an error and parks the request for a human.
- Blocking work goes through `asyncio.to_thread` (routers) or the job pool.
- **A job has at most one live attempt.** `resume()` waits for the previous one
  to actually finish before starting another (`_stop_then_run`), and refuses
  rather than run two if it will not stop. All the state writes in
  `_run_download` depend on this — they used to be guarded by a generation
  counter and are not any more. `tests/test_resume_isolation.py` covers it.
- **Segments are written atomically by the engine**, so a `<n>.ts` in a temp dir
  is always complete. That is why all attempts of a job share one temp
  directory and why resume can trust what it finds there.

### Output layout

```
<library>/
├── Movie (2020)/Movie (2020).mp4
└── Series (2019)/Season 01/Series S01E01.mp4
```

Subtitles land beside the video as `{stem}.{lang}.vtt` (Jellyfin convention). Temp segments go to
`tmp/<job_id>/` and are cleaned up afterwards.

### Quality and languages

Highest available resolution is chosen automatically (1080p → 720p → 480p → 360p). Audio and
subtitle languages are chosen per download or per request.

### vixcloud.co quirk

TV episode M3U8 URLs sometimes return 403; appending `?b=1` (or `&b=1`) resolves it. Handled in
`_fetch_text_with_b1_fallback` and `_collect_audio_tracks`.
