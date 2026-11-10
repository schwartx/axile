//! Fixed-format declaration discovery only: no Python import or evaluation.
use serde::Serialize;

mod context;
mod directory;
mod marker;
pub use directory::scan_directory;

pub fn block_backend() -> &'static str {
    marker::backend()
}

pub fn checked_source(bytes: &[u8], simd: bool) -> Result<&str, std::str::Utf8Error> {
    if simd {
        if let Ok(source) = simdutf8::basic::from_utf8(bytes) {
            return Ok(source);
        }
    }
    // Preserve standard error offsets and lengths on the invalid-input path.
    std::str::from_utf8(bytes)
}

const MARKER: &[u8] = b"@register_algorithm";
const EVENTS: [bool; 256] = {
    let mut table = [false; 256];
    let bytes = b"\n\r#'\"\\()[]{}";
    let mut i = 0;
    while i < bytes.len() {
        table[bytes[i] as usize] = true;
        i += 1;
    }
    table
};
#[derive(Debug, PartialEq, Eq, Serialize)]
pub struct Algorithm {
    pub name: String,
    pub function: String,
    pub offset: usize,
}

#[derive(Debug, Default, PartialEq, Eq, Serialize)]
pub struct Discovery {
    pub algorithms: Vec<Algorithm>,
    pub unsupported: Vec<usize>,
}

struct Pending {
    name: String,
    offset: usize,
}

fn identifier(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_'
}

// Skip a complete ordinary/raw/bytes quoted region, including triple quotes.
// shortcut: Python 3.12 f-string expression grammar is not implemented; validate
// that grammar separately before promoting this experiment to a production scanner.
#[inline(always)]
fn quoted<const FAST: bool>(b: &[u8], start: usize) -> Option<usize> {
    let quote = b[start];
    let triple = b.get(start..start + 3) == Some(&[quote, quote, quote]);
    let width = if triple { 3 } else { 1 };
    let mut i = start + width;
    while i < b.len() {
        if FAST {
            let class = match (quote, triple) {
                (b'\'', false) => &context::SINGLE,
                (b'"', false) => &context::DOUBLE,
                (b'\'', true) => &context::TRIPLE_SINGLE,
                _ => &context::TRIPLE_DOUBLE,
            };
            i = context::next(b, i, b.len(), class);
            if i == b.len() {
                return None;
            }
        }
        match b[i] {
            b'\\' => i = (i + 2).min(b.len()),
            q if q == quote => {
                if !triple || b.get(i..i + 3) == Some(&[quote, quote, quote]) {
                    return Some(i + width);
                }
                i += 1;
            }
            b'\n' | b'\r' if !triple => return None,
            _ => i += 1,
        }
    }
    None
}

fn whitespace<const FAST: bool>(b: &[u8], mut i: usize) -> usize {
    loop {
        while b.get(i).is_some_and(u8::is_ascii_whitespace) {
            i += 1;
        }
        if b.get(i) != Some(&b'#') {
            return i;
        }
        if FAST {
            i = context::next(b, i, b.len(), &context::COMMENT);
        } else {
            while i < b.len() && b[i] != b'\n' {
                i += 1;
            }
        }
    }
}

fn literal_name<const FAST: bool>(b: &[u8], mut i: usize) -> Result<(String, usize), ()> {
    i = whitespace::<FAST>(b, i);
    if !matches!(b.get(i), Some(b'\'' | b'"')) {
        return Err(());
    }
    let start = i + 1;
    let quote = b[i];
    i = start;
    while i < b.len() && b[i] != quote {
        if matches!(b[i], b'\\' | b'\n' | b'\r') {
            return Err(());
        }
        i += 1;
    }
    if i == b.len() || i == start {
        return Err(());
    }
    let name = std::str::from_utf8(&b[start..i])
        .map_err(|_| ())?
        .to_owned();
    i = whitespace::<FAST>(b, i + 1);
    if !matches!(b.get(i), Some(b',' | b')')) {
        return Err(());
    }
    Ok((name, i))
}

fn registration<const FAST: bool>(b: &[u8], offset: usize) -> Result<Option<(String, usize)>, ()> {
    let after = offset + MARKER.len();
    if b.get(after).is_some_and(|v| identifier(*v)) {
        return Ok(None);
    }
    let mut i = after;
    while b.get(i).is_some_and(|v| *v == b' ' || *v == b'\t') {
        i += 1;
    }
    if b.get(i) != Some(&b'(') {
        return Err(());
    }
    let (name, mut i) = literal_name::<FAST>(b, i + 1)?;
    let mut depth = 1usize;
    while i < b.len() {
        match b[i] {
            b'\'' | b'"' => i = quoted::<FAST>(b, i).ok_or(())?,
            b'#' => i = context::next(b, i, b.len(), &context::COMMENT),
            b'(' => {
                depth += 1;
                i += 1;
            }
            b')' => {
                depth -= 1;
                i += 1;
                if depth == 0 {
                    return Ok(Some((name, i)));
                }
            }
            _ => i += 1,
        }
    }
    Err(())
}

struct Scanner<'a, const FAST: bool> {
    bytes: &'a [u8],
    pos: usize,
    bol: bool,
    depth: usize,
    pending: Vec<Pending>,
    result: Discovery,
}

impl<'a, const FAST: bool> Scanner<'a, FAST> {
    fn new(source: &'a str) -> Self {
        Self {
            bytes: source.as_bytes(),
            pos: 0,
            bol: true,
            depth: 0,
            pending: Vec::new(),
            result: Discovery::default(),
        }
    }

    fn bind_function(&mut self, i: usize) {
        let b = self.bytes;
        if b[i..].starts_with(b"def") && b.get(i + 3).is_some_and(|v| *v == b' ' || *v == b'\t') {
            let mut j = i + 3;
            while b.get(j).is_some_and(|v| *v == b' ' || *v == b'\t') {
                j += 1;
            }
            let start = j;
            if b.get(j)
                .is_some_and(|v| v.is_ascii_alphabetic() || *v == b'_')
            {
                while b.get(j).is_some_and(|v| identifier(*v)) {
                    j += 1;
                }
                let end = j;
                while b.get(j).is_some_and(|v| *v == b' ' || *v == b'\t') {
                    j += 1;
                }
                if b.get(j) == Some(&b'(') {
                    for pending in self.pending.drain(..) {
                        self.result.algorithms.push(Algorithm {
                            name: pending.name,
                            function: std::str::from_utf8(&b[start..end]).unwrap().to_owned(),
                            offset: pending.offset,
                        });
                    }
                }
            }
        }
        self.pending.clear();
    }

    #[inline(always)]
    fn declaration(&mut self, discover: bool) -> bool {
        let i = self.pos;
        let b = self.bytes;
        let c = b[i];
        if !self.bol || self.depth != 0 || matches!(c, b'\n' | b'\r' | b' ' | b'\t' | b'#') {
            return false;
        }
        if c == b'@' {
            if discover && b[i..].starts_with(MARKER) {
                match registration::<FAST>(b, i) {
                    Ok(Some((name, end))) => {
                        self.pending.push(Pending { name, offset: i });
                        self.pos = end;
                        self.bol = false;
                        return true;
                    }
                    Err(()) => self.result.unsupported.push(i),
                    Ok(None) => (),
                }
            }
        } else if !self.pending.is_empty() {
            self.bind_function(i);
        }
        false
    }

    #[inline(always)]
    fn step(&mut self, discover: bool) {
        if self.declaration(discover) {
            return;
        }
        let i = self.pos;
        let b = self.bytes;
        let c = b[i];
        match c {
            b'\n' => {
                self.pos += 1;
                self.bol = true;
            }
            b'\r' => self.pos += 1,
            b'#' => {
                self.pos = context::next(b, self.pos, b.len(), &context::COMMENT);
            }
            b'\'' | b'"' => {
                self.pos = quoted::<FAST>(b, i).unwrap_or(b.len());
                self.bol = false;
            }
            b'\\' if b.get(i + 1) == Some(&b'\n') => {
                self.pos += 2;
                self.bol = false;
            }
            b'\\' if b.get(i + 1..i + 3) == Some(b"\r\n") => {
                self.pos += 3;
                self.bol = false;
            }
            b'(' | b'[' | b'{' => {
                self.depth += 1;
                self.pos += 1;
                self.bol = false;
            }
            b')' | b']' | b'}' => {
                self.depth = self.depth.saturating_sub(1);
                self.pos += 1;
                self.bol = false;
            }
            _ => {
                self.pos += 1;
                self.bol = false;
            }
        }
    }

    #[inline(always)]
    fn advance(&mut self, target: usize, discover: bool) {
        while self.pos < target && self.pos < self.bytes.len() {
            // Ordinary bytes cannot change lexical context between line starts.
            // Keep this hot loop independent of the larger transition handler.
            if !self.bol {
                if FAST {
                    self.pos = context::next(self.bytes, self.pos, target, &context::CODE);
                } else {
                    while self.pos < target && !EVENTS[self.bytes[self.pos] as usize] {
                        self.pos += 1;
                    }
                }
                if self.pos == target {
                    break;
                }
            }
            self.step(discover);
        }
    }

    fn candidate(&mut self, offset: usize) {
        self.advance(offset, false);
        if self.pos == offset {
            self.step(true);
        }
    }

    fn finish(mut self) -> Discovery {
        // Once there is no pending declaration, no remaining context is needed:
        // the external locator already established that no markers remain.
        while self.pos < self.bytes.len() && !self.pending.is_empty() {
            self.step(false);
        }
        self.result
    }
}

fn block<const FAST: bool>(source: &str) -> Discovery {
    let mut scanner = Scanner::<FAST>::new(source);
    let mut cursor = 0;
    while let Some(offset) = marker::next(source.as_bytes(), cursor) {
        scanner.candidate(offset);
        cursor = scanner.pos.max(offset + MARKER.len());
    }
    scanner.finish()
}

pub fn discover(source: &str) -> Discovery {
    block::<true>(source)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn utf8_short_inputs_match_standard_errors() {
        for first in 0..=255u8 {
            assert_eq!(
                checked_source(&[first], true),
                std::str::from_utf8(&[first])
            );
            for second in 0..=255u8 {
                let bytes = [first, second];
                assert_eq!(checked_source(&bytes, true), std::str::from_utf8(&bytes));
            }
        }
    }

    #[test]
    fn utf8_valid_invalid_and_split_boundaries() {
        let payloads: &[&[u8]] = &[
            "中文和🙂".as_bytes(),
            b"\xc0\xaf",
            b"\xed\xa0\x80",
            b"\xf4\x90\x80\x80",
            b"\xe4\xb8",
            b"\x80",
        ];
        for prefix in 0..130 {
            for payload in payloads {
                let mut bytes = vec![b'a'; prefix];
                bytes.extend_from_slice(payload);
                bytes.extend_from_slice(&[b'x'; 130]);
                assert_eq!(checked_source(&bytes, true), std::str::from_utf8(&bytes));
            }
        }
    }

    fn check(source: &str, expected: &[(&str, &str)]) {
        let result = discover(source);
        assert_eq!(
            result
                .algorithms
                .iter()
                .map(|a| (a.name.as_str(), a.function.as_str()))
                .collect::<Vec<_>>(),
            expected
        );
        assert!(result.unsupported.is_empty(), "{result:?}");
    }
    #[test]
    fn real_style_multiline() {
        check("@register_algorithm(\n 'PREV-VWAP-OPEN',\n channels=['gm'],\n params_class=P,\n label='昨均价开盘执行',\n)\ndef opening(executor, data):\n pass\n", &[("PREV-VWAP-OPEN", "opening")]);
    }
    #[test]
    fn multiple() {
        check("@register_algorithm('A')\ndef a(e, i): pass\n@register_algorithm(\"B\")\ndef b(e, i): pass\n", &[("A", "a"), ("B", "b")]);
    }
    #[test]
    fn comments_and_docstrings() {
        check("# @register_algorithm('BAD')\n'''\n@register_algorithm('BAD')\ndef fake(e, i): pass\n'''\n\"\"\"\n@register_algorithm('BAD')\n\"\"\"\n@register_algorithm('OK')\ndef real(e, i): pass\n", &[("OK", "real")]);
    }
    #[test]
    fn escaped_and_raw_strings() {
        check("a = r\"escaped \\\" @register_algorithm('BAD')\"\nb = \"@register_algorithm('BAD')\"\n@register_algorithm('OK')\ndef f(e, i): pass\n", &[("OK", "f")]);
    }
    #[test]
    fn nested_functions_and_conditional() {
        check("def outer():\n @register_algorithm('NESTED')\n def inner(e, i): pass\nif False:\n @register_algorithm('CONDITIONAL')\n def hidden(e, i): pass\n", &[]);
    }
    #[test]
    fn implicit_continuation() {
        check("x = (\n matrix\n@register_algorithm('BAD')\n)\n", &[]);
    }
    #[test]
    fn explicit_continuation() {
        check("x = matrix \\\n@register_algorithm('BAD')\n", &[]);
    }
    #[test]
    fn crlf_continuation() {
        check("x = matrix \\\r\n@register_algorithm('BAD')\r\n", &[]);
    }
    #[test]
    fn stacked_decorators() {
        check(
            "@other\n@register_algorithm('A')\n@wrap(1)\ndef f(e, i): pass\n",
            &[("A", "f")],
        );
    }
    #[test]
    fn stacked_registration() {
        check(
            "@register_algorithm('A')\n@register_algorithm('B')\ndef f(e, i): pass\n",
            &[("A", "f"), ("B", "f")],
        );
    }
    #[test]
    fn comments_in_declaration() {
        check("@register_algorithm( # one\n # two\n 'A', label=\") text\", default_params={'x': (1, 2)},\n) # done\n# doc\ndef f(e, i): pass\n", &[("A", "f")]);
    }
    #[test]
    fn prefix_collision() {
        check("@register_algorithm_extra('BAD')\ndef f(e, i): pass\n", &[]);
    }
    #[test]
    fn missing_function() {
        check("@register_algorithm('A')\nx = 1\ndef f(e, i): pass\n", &[]);
    }
    #[test]
    fn dynamic_is_reported() {
        {
            let r = discover("@register_algorithm(make_name())\ndef f(e, i): pass\n");
            assert!(r.algorithms.is_empty());
            assert_eq!(r.unsupported, vec![0]);
        }
    }
    #[test]
    fn helper_is_not_algorithm() {
        check(
            "from axile import register_algorithm\ndef helper(): return 1\n",
            &[],
        );
    }
    #[test]
    fn malformed_header() {
        {
            let r = discover("@register_algorithm('A', label=(\n");
            assert!(r.algorithms.is_empty());
            assert_eq!(r.unsupported, vec![0]);
        }
    }
    #[test]
    fn unicode_name() {
        check(
            "@register_algorithm('算法甲')\ndef algo(e, i): pass\n",
            &[("算法甲", "algo")],
        );
    }
    #[test]
    fn multiple_prefixed_strings() {
        check("doc = b'''\n@register_algorithm('BAD')\n'''\nr = r'''\n@register_algorithm('BAD')\n'''\n@register_algorithm('OK')\ndef f(e, i): pass\n", &[("OK", "f")]);
    }
}
