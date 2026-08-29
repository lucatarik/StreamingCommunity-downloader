# sc-dl-engine

The HLS download engine for the panel. Reads one job on stdin, reports events on
stdout, logs to stderr.

```bash
sc-dl-engine < job.json          # or: sc-dl-engine job.json
```

## Why a separate process

A blocking network call cannot be killed from outside its thread in Python. The
panel's old in-process downloader could therefore only *ask* an attempt to stop;
one wedged on a socket kept running and would later write its stale result over
a newer attempt's. Everything built to survive that — generation counters,
per-generation temp folders, hardlink migration between them, an "am I still the
current attempt?" check before every state write — existed only because the stop
could not be enforced.

A child process can be killed. Cancellation is now a fact, so the panel assumes
one live attempt per job and none of the above is needed.

## Input

```json
{
  "m3u8_index": "https://cdn.example/v/master.m3u8",
  "output_path": "/library/Film (2020)/Film (2020).mp4",
  "temp_dir": "tmp/<job-id>",
  "key_hex": "00112233445566778899aabbccddeeff",
  "referer": "https://vixcloud.co/embed/1234?token=...",
  "audio_tracks":    [{ "url": "https://…/audio-ita.m3u8", "language": "ita" }],
  "subtitle_tracks": [{ "uri": "https://…/sub-ita.m3u8",   "language": "ita" }],
  "subtitle_languages": ["ita", "eng"],
  "max_segment_workers": 16,
  "user_agent": "Mozilla/5.0 …"
}
```

Only `m3u8_index`, `output_path` and `temp_dir` are required. `m3u8_index` may
be a master or a media playlist — the engine follows one hop if needed and picks
the highest-bandwidth variant. `key_hex` is applied only if the playlist
declares `EXT-X-KEY:METHOD=AES-128`; a key given for an unencrypted stream is
ignored rather than used. `subtitle_languages` are looked up among the master
playlist's own subtitle renditions and merged with `subtitle_tracks`.

## Output

Newline-delimited JSON on stdout, one object per line:

| `type`      | fields                                                            |
|-------------|-------------------------------------------------------------------|
| `progress`  | `phase`, `current`, `total`, `pct`, `speed`, `bytes_speed`, `eta` |
| `status`    | `phase` — `joining`, `merging`, `audio_<lang>`                    |
| `done`      | `output_path` — the real path, `.mkv` if a remux happened         |
| `error`     | `message`                                                         |
| `cancelled` | —                                                                 |

Exit codes: `0` done, `1` failed, `2` malformed spec, `130` cancelled.

`SC_ENGINE_LOG` sets the stderr log filter (default `info`).

## Guarantees

* **A `<n>.ts` in a segment directory is complete.** Segments are written to
  `<n>.ts.part`, flushed, then renamed. Nothing else creates a `.ts` there. This
  is what makes resume safe without any coordination.
* **The join reads by index, never by directory listing.** A stray file in the
  scratch directory cannot be muxed in as though it were a segment, and a
  missing segment cannot go unnoticed.
* **A missing segment fails the job.** It is never muxed around, so a "done"
  file never has holes in it.
* **Every transient HTTP status is retried** — 408, 429, and all 5xx — with
  jittered backoff that honours `Retry-After`. A 403 gets the vixcloud `?b=1`
  fallback first.
* **Timeouts sit on the gap between body chunks**, not on total elapsed time, so
  a CDN throttled to a trickle is detected instead of hanging forever.
* **A failed or cancelled run keeps its scratch space** and deletes its partial
  output. Only a completed run cleans up.

## Tests

```bash
cargo test              # 35 unit + 7 end-to-end
```

The end-to-end tests run the real binary against a fake CDN that injects 503s,
403s and stalls, then verify the result with `ffprobe`. They need `ffmpeg` and
`ffprobe` on PATH.
