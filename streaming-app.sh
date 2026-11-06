#!/usr/bin/env bash
#
# Avvia il pannello come un'applicazione: una finestra, e quando la chiudi si
# ferma tutto. Niente Docker, niente Jellyfin, niente servizio in background.
#
#   ./streaming-app.sh            avvia
#   ./streaming-app.sh --setup    rifà da zero l'ambiente Python
#   ./streaming-app.sh --no-app   solo il server, apri tu il browser
#
# La prima esecuzione prepara l'ambiente (qualche minuto). Le successive
# partono in pochi secondi.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$HERE"
STATE="${SC_STATE_DIR:-$HOME/.local/share/streaming-downloader}"
VENV="$STATE/venv"
LOG="$STATE/panel.log"

# Dove finiscono i film. Sovrascrivibile: SC_LIBRARY=/percorso ./streaming-app.sh
if [[ -z "${SC_LIBRARY:-}" ]]; then
  if   [[ -d "$HOME/Scaricati" ]]; then SC_LIBRARY="$HOME/Scaricati"
  elif [[ -d "$HOME/Video"     ]]; then SC_LIBRARY="$HOME/Video"
  else SC_LIBRARY="$HOME/Scaricati"; fi
fi

c_info()  { printf '\033[36m→\033[0m %s\n' "$*"; }
c_ok()    { printf '\033[32m✓\033[0m %s\n' "$*"; }
c_warn()  { printf '\033[33m!\033[0m %s\n' "$*"; }
c_err()   { printf '\033[31m✗\033[0m %s\n' "$*" >&2; }

[[ -d "$REPO" ]] || { c_err "Non trovo il progetto in $REPO"; exit 1; }
mkdir -p "$STATE" "$SC_LIBRARY"

# ── Ambiente Python ─────────────────────────────────────────────────────────
# Le dipendenze si reinstallano solo quando requirements.txt cambia davvero,
# così un avvio normale non paga un giro di pip.
REQ="$REPO/requirements.txt"
STAMP="$STATE/.deps-stamp"
WANT="$(sha256sum "$REQ" | cut -d' ' -f1)"

if [[ "${1:-}" == "--setup" ]]; then rm -rf "$VENV" "$STAMP"; shift; fi

if [[ ! -x "$VENV/bin/python" ]]; then
  c_info "Preparo l'ambiente Python (solo la prima volta)…"
  python3 -m venv "$VENV"
fi
if [[ ! -f "$STAMP" || "$(cat "$STAMP")" != "$WANT" ]]; then
  c_info "Installo le dipendenze…"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -r "$REQ"
  printf '%s' "$WANT" > "$STAMP"
  c_ok "Dipendenze pronte"
fi

# ── Motore di download ──────────────────────────────────────────────────────
# Il binario vive in $STATE/bin; la compilazione avviene in una cartella
# temporanea che sparisce subito dopo, così non resta nulla in engine/target.
ENGINE="$STATE/bin/sc-dl-engine"
if [[ ! -x "$ENGINE" ]]; then
  mkdir -p "$STATE/bin"
  LEGACY="$REPO/engine/target/release/sc-dl-engine"
  if [[ -x "$LEGACY" ]]; then
    cp "$LEGACY" "$ENGINE"
    rm -rf "$REPO/engine/target"
  elif command -v cargo >/dev/null 2>&1; then
    c_info "Compilo il motore di download (solo la prima volta, qualche minuto)…"
    BUILD="$(mktemp -d)"
    if CARGO_TARGET_DIR="$BUILD" cargo build --release --quiet --manifest-path "$REPO/engine/Cargo.toml"; then
      cp "$BUILD/release/sc-dl-engine" "$ENGINE"
      rm -rf "$BUILD"
      c_ok "Motore compilato"
    else
      rm -rf "$BUILD"
      c_err "Compilazione del motore fallita."
      exit 1
    fi
  else
    c_err "Serve Rust per compilare il motore: https://rustup.rs"
    c_err "Oppure installa sc-dl-engine nel PATH."
    exit 1
  fi
fi

# ── Una porta libera ────────────────────────────────────────────────────────
# Fissa se possibile, così il segnalibro resta valido; altrimenti la prima
# libera, invece di fallire perché qualcosa occupa la 8000.
pick_port() {
  local p
  for p in 8000 8001 8002 8010 8080 0; do
    [[ $p == 0 ]] && { python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()'; return; }
    python3 -c "import socket,sys;s=socket.socket();sys.exit(0 if s.connect_ex(('127.0.0.1',$p)) else 1)" && { echo "$p"; return; }
  done
}
PORT="$(pick_port)"
URL="http://127.0.0.1:$PORT"

# ── Avvio del server ────────────────────────────────────────────────────────
SERVER_PID=""
BROWSER_PID=""
BROWSER_DIR=""

cleanup() {
  trap - EXIT INT TERM
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    # Un download in corso viene interrotto, ma non perso: il motore tiene i
    # segmenti già scaricati e "Forza ripresa" riparte da lì.
    local running
    running="$(curl -s --max-time 2 "$URL/api/jobs" 2>/dev/null \
      | python3 -c 'import sys,json;print(sum(1 for j in json.load(sys.stdin) if j["status"] in ("running","queued")))' 2>/dev/null || echo 0)"
    [[ "$running" -gt 0 ]] 2>/dev/null && \
      c_warn "$running download interrotti — riaprendo l'app, \"Forza ripresa\" li riprende da dove erano."
    c_info "Chiudo il pannello…"
    kill -TERM "$SERVER_PID" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$SERVER_PID" 2>/dev/null || break; sleep 0.25; done
    kill -KILL "$SERVER_PID" 2>/dev/null || true
  fi
  [[ -n "$BROWSER_PID" ]] && kill -TERM "$BROWSER_PID" 2>/dev/null || true
  [[ -n "$BROWSER_DIR" ]] && rm -rf "$BROWSER_DIR"
  c_ok "Fermato."
}
trap cleanup EXIT INT TERM

c_info "Avvio il pannello su $URL"
(
  cd "$REPO"
  DB_FILE="$STATE/panel.db" DATA_FILE="$STATE/data.json" \
  SCHEDULE_FILE="$STATE/schedule.json" TMP_DIR="$STATE/tmp" \
  VIDEOS_DIR="$SC_LIBRARY" HOST=127.0.0.1 PORT="$PORT" \
  SC_ENGINE_BIN="$ENGINE" \
  exec "$VENV/bin/python" main.py
) >"$LOG" 2>&1 &
SERVER_PID=$!

# Aspetta che risponda, ma non per sempre: se è morto all'avvio, mostra perché.
for i in $(seq 1 60); do
  kill -0 "$SERVER_PID" 2>/dev/null || { c_err "Il pannello non è partito:"; tail -20 "$LOG" >&2; exit 1; }
  curl -s -o /dev/null --max-time 2 "$URL" && break
  sleep 0.5
  [[ $i == 60 ]] && { c_err "Il pannello non risponde. Log in $LOG"; exit 1; }
done
c_ok "Pannello pronto — libreria: $SC_LIBRARY"

if [[ "${1:-}" == "--no-app" ]]; then
  c_info "Apri $URL nel browser. Ctrl+C per fermare."
  wait "$SERVER_PID"
  exit 0
fi

# ── La finestra ─────────────────────────────────────────────────────────────
# --user-data-dir dedicato non è un dettaglio: senza, Chrome consegna l'URL
# all'istanza già aperta e il comando ritorna subito, quindi chiudere la
# finestra non fermerebbe nulla. Con un profilo suo, resta in attesa.
BROWSER=""
for b in google-chrome-stable google-chrome chromium brave-browser brave vivaldi microsoft-edge; do
  command -v "$b" >/dev/null 2>&1 && { BROWSER="$b"; break; }
done

if [[ -n "$BROWSER" ]]; then
  BROWSER_DIR="$(mktemp -d)"
  c_info "Apro la finestra ($BROWSER). Chiudila per fermare tutto."
  "$BROWSER" --app="$URL/?appwindow=1" \
             --user-data-dir="$BROWSER_DIR" \
             --class=StreamingDownloader \
             --no-first-run --no-default-browser-check \
             >/dev/null 2>&1 &
  BROWSER_PID=$!
  wait "$BROWSER_PID" || true
  BROWSER_PID=""
else
  c_warn "Nessun browser Chrome/Chromium: apro quello predefinito."
  c_warn "Chiudere la scheda non fermerà il pannello — usa Ctrl+C qui."
  xdg-open "$URL" >/dev/null 2>&1 || true
  wait "$SERVER_PID"
fi
