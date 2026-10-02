//! Saving a file the UI downloaded from agentd (an experiment's export zip).
//!
//! The webview never gets a filesystem or dialog permission: it sends the bytes and a
//! suggested name to `save_file`, the user picks the path in a native save dialog, and
//! only that path is written. The name is checked to be a bare file name.

use std::fmt;

use tauri::http::HeaderMap;
use tauri::ipc::InvokeBody;

/// The header carrying the suggested name, percent-encoded (`encodeURIComponent`) so
/// any Unicode title fits in an ASCII header value.
pub const NAME_HEADER: &str = "x-newton-file-name";

const MAX_NAME_BYTES: usize = 255;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NameError(pub String);

impl fmt::Display for NameError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// A bare file name: no directories, no control characters, not `.` or `..`.
pub fn check_file_name(name: &str) -> Result<&str, NameError> {
    let bad = |why: &str| {
        Err(NameError(format!(
            "can't suggest {name:?} as a file name: {why}"
        )))
    };
    if name.trim().is_empty() {
        return bad("it is empty");
    }
    if name.len() > MAX_NAME_BYTES {
        return bad("it is too long");
    }
    if name == "." || name == ".." {
        return bad("it names a directory");
    }
    if name.contains(['/', '\\', ':']) {
        return bad("it contains a path separator");
    }
    if name.chars().any(char::is_control) {
        return bad("it contains a control character");
    }
    Ok(name)
}

/// What a `save_file` call carries: the raw body's bytes and the checked name from
/// `NAME_HEADER` (the shape `platform.ts` `saveFile` sends).
pub fn save_request(
    body: &InvokeBody,
    headers: &HeaderMap,
) -> Result<(Vec<u8>, String), NameError> {
    let InvokeBody::Raw(bytes) = body else {
        return Err(NameError(
            "save_file expects the file's bytes as a raw body".into(),
        ));
    };
    let header = headers.get(NAME_HEADER);
    let encoded = header.and_then(|v| v.to_str().ok()).unwrap_or_default();
    let name = percent_decode(encoded)?;
    check_file_name(&name)?;
    Ok((bytes.clone(), name))
}

/// Decodes `%XX` escapes (what `encodeURIComponent` produces) into UTF-8 text.
pub fn percent_decode(s: &str) -> Result<String, NameError> {
    let invalid = || NameError(format!("{s:?} is not a percent-encoded name"));
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' {
            let hex = bytes.get(i + 1..i + 3).ok_or_else(invalid)?;
            let hex = std::str::from_utf8(hex).map_err(|_| invalid())?;
            out.push(u8::from_str_radix(hex, 16).map_err(|_| invalid())?);
            i += 3;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    String::from_utf8(out).map_err(|_| invalid())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tauri::http::HeaderValue;

    fn named(value: &str) -> HeaderMap {
        let mut headers = HeaderMap::new();
        headers.insert(NAME_HEADER, HeaderValue::from_str(value).unwrap());
        headers
    }

    #[test]
    fn name_header_matches_the_ui() {
        // apps/desktop/src/app/platform.ts FILE_NAME_HEADER (pinned in platform.test.ts).
        assert_eq!(NAME_HEADER, "x-newton-file-name");
    }

    #[test]
    fn reads_raw_bytes_and_the_named_header() {
        let body = InvokeBody::Raw(vec![1, 2, 3]);
        let (bytes, name) = save_request(&body, &named("Sch%C3%A9ma%20TVD-exp-1.zip")).unwrap();
        assert_eq!(bytes, vec![1, 2, 3]);
        assert_eq!(name, "Schéma TVD-exp-1.zip");
    }

    #[test]
    fn refuses_json_bodies_and_missing_or_bad_names() {
        let json = InvokeBody::Json(serde_json::json!({ "bytes": [1, 2, 3] }));
        let err = save_request(&json, &named("a.zip")).unwrap_err();
        assert!(err.0.contains("raw body"), "{err}");

        let raw = InvokeBody::Raw(vec![1]);
        let err = save_request(&raw, &HeaderMap::new()).unwrap_err();
        assert!(err.0.contains("it is empty"), "{err}");
        let err = save_request(&raw, &named("..%2Fx.zip")).unwrap_err();
        assert!(err.0.contains("path separator"), "{err}");
    }

    #[test]
    fn accepts_bare_names() {
        assert_eq!(check_file_name("report-exp-1.zip"), Ok("report-exp-1.zip"));
        assert_eq!(check_file_name("Schéma TVD.zip"), Ok("Schéma TVD.zip"));
        assert_eq!(check_file_name(".hidden"), Ok(".hidden"));
    }

    #[test]
    fn rejects_paths_and_odd_names() {
        for name in [
            "",
            "  ",
            ".",
            "..",
            "../x.zip",
            "a/b.zip",
            "/etc/passwd",
            "a\\b.zip",
            "C:x.zip",
            "a\nb.zip",
            "a\0b.zip",
        ] {
            assert!(
                check_file_name(name).is_err(),
                "{name:?} should be rejected"
            );
        }
        assert!(check_file_name(&"a".repeat(256)).is_err());
        assert!(check_file_name(&"a".repeat(255)).is_ok());
    }

    #[test]
    fn decodes_encode_uri_component() {
        assert_eq!(percent_decode("report.zip").unwrap(), "report.zip");
        assert_eq!(
            percent_decode("Sch%C3%A9ma%20TVD.zip").unwrap(),
            "Schéma TVD.zip"
        );
        // An encoded slash decodes, and the name check then refuses it.
        let name = percent_decode("..%2Fx.zip").unwrap();
        assert!(check_file_name(&name).is_err());
        assert!(percent_decode("bad%2").is_err());
        assert!(percent_decode("bad%zz").is_err());
        assert!(percent_decode("%FF").is_err());
    }
}
