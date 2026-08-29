# ── Stage 1: the download engine ───────────────────────────────────────────
# Built separately so the runtime image carries the binary and none of the
# Rust toolchain. Dependencies are compiled before the sources are copied, so
# editing the engine does not re-download and rebuild the whole crate graph.
FROM rust:1-slim-bookworm AS engine

WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends pkg-config && \
    rm -rf /var/lib/apt/lists/*

COPY engine/Cargo.toml engine/Cargo.lock ./
RUN mkdir src && echo 'fn main() {}' > src/main.rs && \
    cargo build --release --locked && \
    rm -rf src

COPY engine/src ./src
# Cargo keys rebuilds off mtime; the placeholder above shares a timestamp with
# the real main.rs closely enough that touch is the reliable way to invalidate.
RUN touch src/main.rs && cargo build --release --locked


# ── Stage 2: the panel ─────────────────────────────────────────────────────
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=engine /build/target/release/sc-dl-engine /usr/local/bin/sc-dl-engine

COPY . .

# Fail at build time rather than on the first download if the binary did not
# make it across.
RUN sc-dl-engine --help >/dev/null 2>&1 || test -x /usr/local/bin/sc-dl-engine

EXPOSE 8000

CMD ["python", "main.py"]
