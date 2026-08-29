//! Language code handling, ported from `app/core/format.py`.
//!
//! vixcloud emits ISO 639-2/B codes plus a `forced-<code>` variant. Matroska
//! needs a real three-letter code, so `forced-ita` has to become
//! `language=ita` plus a forced disposition — writing it through verbatim is
//! what made players show those tracks as "Undefined".

/// ISO 639-2 → the short code used in the `{stem}.{lang}.vtt` filenames that
/// Jellyfin picks up.
pub fn short_code(code: &str) -> &str {
    match code {
        "ita" => "it",
        "eng" => "en",
        "fra" | "fre" => "fr",
        "spa" => "es",
        "deu" | "ger" => "de",
        "por" => "pt",
        "jpn" => "ja",
        "zho" => "zh",
        "ara" => "ar",
        "rus" => "ru",
        "kor" => "ko",
        "nld" => "nl",
        "pol" => "pl",
        "swe" => "sv",
        "nor" => "no",
        "dan" => "da",
        "fin" => "fi",
        "tur" => "tr",
        "ell" | "gre" => "el",
        "ces" => "cs",
        other => other,
    }
}

/// Human-readable track title for the Matroska metadata.
pub fn display_name(code: &str) -> String {
    let name = match code {
        "ita" => "Italian",
        "eng" => "English",
        "fra" | "fre" => "French",
        "spa" => "Spanish",
        "deu" | "ger" => "German",
        "por" => "Portuguese",
        "jpn" => "Japanese",
        "rus" => "Russian",
        "ukr" => "Ukrainian",
        "ell" | "gre" => "Greek",
        "nld" => "Dutch",
        "pol" => "Polish",
        "ara" => "Arabic",
        "zho" => "Chinese",
        "kor" => "Korean",
        other => return other.to_uppercase(),
    };
    name.to_string()
}

/// Split a raw vixcloud language token into `(code, forced)`.
pub fn normalize(raw: &str) -> (&str, bool) {
    match raw.strip_prefix("forced-") {
        Some(code) if !code.is_empty() => (code, true),
        Some(_) => ("und", true),
        None if raw.is_empty() => ("und", false),
        None => (raw, false),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn forced_variants_split_into_code_and_flag() {
        assert_eq!(normalize("forced-ita"), ("ita", true));
        assert_eq!(normalize("ita"), ("ita", false));
        assert_eq!(normalize(""), ("und", false));
        assert_eq!(normalize("forced-"), ("und", true));
    }

    #[test]
    fn unknown_codes_pass_through_rather_than_becoming_und() {
        assert_eq!(short_code("xyz"), "xyz");
        assert_eq!(display_name("xyz"), "XYZ");
    }

    #[test]
    fn both_iso_639_2_variants_map_to_the_same_short_code() {
        assert_eq!(short_code("fra"), short_code("fre"));
        assert_eq!(short_code("deu"), short_code("ger"));
        assert_eq!(display_name("gre"), display_name("ell"));
    }
}
