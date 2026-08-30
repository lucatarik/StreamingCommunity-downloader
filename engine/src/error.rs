use std::path::PathBuf;

/// Cancellation is a variant rather than a separate channel so every `?` in the
/// pipeline propagates it correctly. The old Python engine used an exception for
/// this too, but any bare `except Exception` swallowed it into a generic error;
/// here the compiler forces every handler to name the variant it is treating.
#[derive(Debug, thiserror::Error)]
pub enum EngineError {
    #[error("annullato")]
    Cancelled,

    #[error("{context}: HTTP {status} dopo {attempts} tentativi")]
    HttpStatus {
        context: String,
        status: u16,
        attempts: u32,
    },

    #[error("{context}: {source}")]
    Http {
        context: String,
        #[source]
        source: reqwest::Error,
    },

    #[error("{context}: nessun byte ricevuto entro {secs}s (CDN al rallentatore)")]
    Stalled { context: String, secs: u64 },

    #[error("playlist M3U8 non valida ({context}): {detail}")]
    Playlist { context: String, detail: String },

    /// The failure the old `join()` used to paper over: it logged a warning and
    /// muxed the surviving segments anyway, handing the user a file with silent
    /// gaps in it. Missing segments are now fatal, always.
    #[error(
        "{count} segmenti non scaricabili dopo i retry (indici: {preview}). \
         File non completo: usa \"Forza ripresa\" per riscaricare solo i segmenti mancanti."
    )]
    MissingSegments { count: usize, preview: String },

    #[error("ffmpeg ({stage}) uscito con {code}: {stderr}")]
    Ffmpeg {
        stage: String,
        code: String,
        stderr: String,
    },

    #[error("ffmpeg non trovato nel PATH")]
    FfmpegMissing,

    /// The source is refusing outright rather than shedding load. Distinct from
    /// MissingSegments because the advice differs: retrying now is pointless,
    /// where a handful of lost segments is exactly what a resume repairs.
    #[error(
        "La fonte ha rifiutato {streak} segmenti di fila e non sta servendo nulla. \
         Download interrotto dopo {done} segmenti su {total}: riprova più tardi, \
         oppure usa \"Forza ripresa\" per continuare da qui."
    )]
    SourceRefusing {
        streak: usize,
        done: usize,
        total: usize,
    },

    #[error("errore su {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },

    #[error("chiave AES non valida: {0}")]
    BadKey(String),
}

pub type Result<T> = std::result::Result<T, EngineError>;

/// `std::io::Error` carries no path, which turns every I/O failure report into a
/// guessing game. This attaches one at the call site.
pub trait IoContext<T> {
    fn at(self, path: impl Into<PathBuf>) -> Result<T>;
}

impl<T> IoContext<T> for std::result::Result<T, std::io::Error> {
    fn at(self, path: impl Into<PathBuf>) -> Result<T> {
        self.map_err(|source| EngineError::Io {
            path: path.into(),
            source,
        })
    }
}
