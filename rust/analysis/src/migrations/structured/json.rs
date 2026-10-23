//! Python 历史 JSON 的非有限数只在词法层放行，字符串保持原义。

use std::collections::{BTreeMap, BTreeSet};

use super::*;

enum Piece<'a> {
    Source(&'a str),
    Number(&'static str),
}

pub(super) fn parse_bytes(source: &[u8]) -> Result<Value> {
    let (body, width, little) = if source.starts_with(&[0xff, 0xfe, 0, 0]) {
        (&source[4..], 4, true)
    } else if source.starts_with(&[0, 0, 0xfe, 0xff]) {
        (&source[4..], 4, false)
    } else if source.starts_with(&[0xff, 0xfe]) {
        (&source[2..], 2, true)
    } else if source.starts_with(&[0xfe, 0xff]) {
        (&source[2..], 2, false)
    } else if source.starts_with(&[0xef, 0xbb, 0xbf]) {
        (&source[3..], 1, false)
    } else if source.len() >= 4 && source[0] == 0 {
        (source, if source[1] == 0 { 4 } else { 2 }, false)
    } else if source.len() >= 4 && source[1] == 0 {
        (
            source,
            if source[2] == 0 && source[3] == 0 {
                4
            } else {
                2
            },
            true,
        )
    } else if source.len() == 2 && source[0] == 0 {
        (source, 2, false)
    } else if source.len() == 2 && source[1] == 0 {
        (source, 2, true)
    } else {
        (source, 1, false)
    };
    let invalid = || error("Invalid historical JSON encoding");
    let text = match width {
        1 => std::str::from_utf8(body).map_err(|_| invalid())?.to_owned(),
        2 => {
            if body.len() % 2 != 0 {
                return Err(invalid());
            }
            let units = body.chunks_exact(2).map(|chunk| {
                if little {
                    u16::from_le_bytes([chunk[0], chunk[1]])
                } else {
                    u16::from_be_bytes([chunk[0], chunk[1]])
                }
            });
            char::decode_utf16(units)
                .collect::<std::result::Result<String, _>>()
                .map_err(|_| invalid())?
        }
        _ => {
            if body.len() % 4 != 0 {
                return Err(invalid());
            }
            body.chunks_exact(4)
                .map(|chunk| {
                    let bytes = [chunk[0], chunk[1], chunk[2], chunk[3]];
                    char::from_u32(if little {
                        u32::from_le_bytes(bytes)
                    } else {
                        u32::from_be_bytes(bytes)
                    })
                    .ok_or_else(invalid)
                })
                .collect::<Result<String>>()?
        }
    };
    parse(&text)
}

pub(super) fn parse(source: &str) -> Result<Value> {
    let bytes = source.as_bytes();
    let mut pieces = Vec::new();
    let mut strings = BTreeSet::new();
    let mut index = 0;
    let mut start = 0;
    while index < bytes.len() {
        if bytes[index] == b'"' {
            let begin = index;
            index += 1;
            while index < bytes.len() {
                match bytes[index] {
                    b'\\' => index += 2,
                    b'"' => {
                        index += 1;
                        break;
                    }
                    _ => index += 1,
                }
            }
            let encoded = source
                .get(begin..index)
                .ok_or_else(|| error("Invalid historical JSON string"))?;
            strings.insert(
                serde_json::from_str::<String>(encoded)
                    .map_err(|_| error("Invalid historical JSON string"))?,
            );
            continue;
        }
        let token = [
            ("-Infinity", "-Infinity"),
            ("Infinity", "Infinity"),
            ("NaN", "NaN"),
        ]
        .into_iter()
        .find(|(token, _)| source[index..].starts_with(token));
        if let Some((token, number)) = token {
            if source[index + token.len()..].trim_start().starts_with(':') {
                return Err(error("Invalid historical JSON object key"));
            }
            pieces.push(Piece::Source(&source[start..index]));
            pieces.push(Piece::Number(number));
            index += token.len();
            start = index;
        } else {
            index += 1;
            while index < bytes.len() && !source.is_char_boundary(index) {
                index += 1;
            }
        }
    }
    if pieces.is_empty() {
        return serde_json::from_str(source).map_err(|_| error("Invalid historical JSON"));
    }
    pieces.push(Piece::Source(&source[start..]));
    let mut markers = BTreeMap::new();
    let mut result = String::with_capacity(source.len());
    let mut counter = 0;
    for piece in pieces {
        match piece {
            Piece::Source(text) => result.push_str(text),
            Piece::Number(number) => {
                let marker = loop {
                    let candidate = format!("\u{0}axile-historical-number:{counter}");
                    counter += 1;
                    if !strings.contains(&candidate) {
                        break candidate;
                    }
                };
                result.push_str(
                    &serde_json::to_string(&marker)
                        .map_err(|failure| error(failure.to_string()))?,
                );
                markers.insert(marker, number);
            }
        }
    }
    let mut value: Value =
        serde_json::from_str(&result).map_err(|_| error("Invalid historical JSON"))?;
    restore_numbers(&mut value, &markers);
    Ok(value)
}

fn restore_numbers(value: &mut Value, markers: &BTreeMap<String, &str>) {
    match value {
        Value::String(text) => {
            if let Some(number) = markers.get(text) {
                *value = Value::Number(serde_json::Number::from_string_unchecked((*number).into()));
            }
        }
        Value::Array(values) => {
            for value in values {
                restore_numbers(value, markers);
            }
        }
        Value::Object(values) => {
            for value in values.values_mut() {
                restore_numbers(value, markers);
            }
        }
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn historical_numbers_distinguish_json_tokens_strings_and_user_markers() {
        let value = parse(r#"{"n":NaN,"p":Infinity,"m":-Infinity,"s":"NaN Infinity -Infinity","collision":"\u0000axile-historical-number:0","ordinary":{"invalid_number":"nan"},"huge":123456789012345678901234567890,"overflow":1e400}"#).unwrap();
        assert!(value["n"].is_number());
        assert_eq!(value["n"].to_string(), "NaN");
        assert_eq!(value["p"].to_string(), "Infinity");
        assert_eq!(value["m"].to_string(), "-Infinity");
        assert_eq!(value["s"], "NaN Infinity -Infinity");
        assert_eq!(value["collision"], "\u{0}axile-historical-number:0");
        assert!(value["ordinary"].is_object());
        assert_eq!(value["huge"].to_string(), "123456789012345678901234567890");
        assert!(value["overflow"].as_f64().is_none());
    }

    #[test]
    fn malformed_nonfinite_spellings_still_fail() {
        for source in ["nan", "+Infinity", "NaNx", "{NaN:0}", "\"unfinished"] {
            assert!(parse(source).is_err(), "{source}");
        }
    }

    #[test]
    fn historical_blob_json_uses_python_encoding_detection() {
        assert_eq!(parse_bytes(&[0xff, 0xfe, b'1', 0]).unwrap(), Value::from(1));
        assert_eq!(parse_bytes(&[0, 0, 0, b'1']).unwrap(), Value::from(1));
        assert_eq!(
            parse_bytes(&[0xef, 0xbb, 0xbf, b'1']).unwrap(),
            Value::from(1)
        );
        assert!(parse_bytes(&[0xff, 0xfe, 0xff]).is_err());
    }
}
