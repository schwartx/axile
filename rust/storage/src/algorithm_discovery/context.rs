//! Bounded skipping of the exact ASCII events needed by the declaration scanner.
use super::EVENTS;

pub(crate) struct Class {
    table: [bool; 256],
    low: [u8; 32],
    high: [u8; 32],
}

impl Class {
    const fn from_table(table: [bool; 256]) -> Self {
        let mut low = [0; 32];
        let mut high = [0; 32];
        let mut byte = 0;
        while byte < 256 {
            if table[byte] {
                assert!(byte < 128);
                low[byte & 15] |= 1 << (byte >> 4);
                low[16 + (byte & 15)] |= 1 << (byte >> 4);
            }
            byte += 1;
        }
        let mut i = 0;
        while i < 8 {
            high[i] = 1 << i;
            high[16 + i] = 1 << i;
            i += 1;
        }
        Self { table, low, high }
    }

    const fn new(events: &[u8]) -> Self {
        let mut table = [false; 256];
        let mut i = 0;
        while i < events.len() {
            table[events[i] as usize] = true;
            i += 1;
        }
        Self::from_table(table)
    }
}

pub(crate) const CODE: Class = Class::from_table(EVENTS);
pub(crate) const COMMENT: Class = Class::new(b"\n");
pub(crate) const SINGLE: Class = Class::new(b"'\\\r\n");
pub(crate) const DOUBLE: Class = Class::new(b"\"\\\r\n");
pub(crate) const TRIPLE_SINGLE: Class = Class::new(b"'\\");
pub(crate) const TRIPLE_DOUBLE: Class = Class::new(b"\"\\");

pub(crate) fn portable(bytes: &[u8], mut i: usize, end: usize, class: &Class) -> usize {
    while i < end && !class.table[bytes[i] as usize] {
        i += 1;
    }
    i
}

#[cfg(target_arch = "x86_64")]
mod x86 {
    use super::{portable, Class};
    use std::arch::x86_64::*;

    #[target_feature(enable = "avx2")]
    pub(super) unsafe fn next(bytes: &[u8], mut i: usize, end: usize, class: &Class) -> usize {
        // SAFETY: both lookup tables contain 32 bytes; caller checks AVX2.
        let (low, high) = unsafe {
            (
                _mm256_loadu_si256(class.low.as_ptr().cast()),
                _mm256_loadu_si256(class.high.as_ptr().cast()),
            )
        };
        let nibble = _mm256_set1_epi8(15);
        let zero = _mm256_setzero_si256();
        while end - i >= 32 {
            // SAFETY: end <= bytes.len(), so this entire 32-byte load is in bounds.
            let data = unsafe { _mm256_loadu_si256(bytes.as_ptr().add(i).cast()) };
            let lo = _mm256_shuffle_epi8(low, _mm256_and_si256(data, nibble));
            let hi =
                _mm256_shuffle_epi8(high, _mm256_and_si256(_mm256_srli_epi16::<4>(data), nibble));
            let hits =
                !(_mm256_movemask_epi8(_mm256_cmpeq_epi8(_mm256_and_si256(lo, hi), zero)) as u32);
            if hits != 0 {
                return i + hits.trailing_zeros() as usize;
            }
            i += 32;
        }
        portable(bytes, i, end, class)
    }
}

#[inline(always)]
pub(crate) fn next(bytes: &[u8], start: usize, end: usize, class: &Class) -> usize {
    let end = end.min(bytes.len());
    if start >= end {
        return end;
    }
    // Most dictionary keys and punctuation gaps end in the first few bytes.
    let probe_end = end.min(start.saturating_add(4));
    let i = portable(bytes, start, probe_end, class);
    if i < probe_end {
        return i;
    }
    #[cfg(target_arch = "x86_64")]
    if end - i >= 32 && std::is_x86_feature_detected!("avx2") {
        // SAFETY: runtime feature check and bounded range guard all vector loads.
        return unsafe { x86::next(bytes, i, end, class) };
    }
    portable(bytes, i, end, class)
}

#[cfg(test)]
mod tests {
    use super::*;
    const CLASSES: [&Class; 6] = [
        &CODE,
        &COMMENT,
        &SINGLE,
        &DOUBLE,
        &TRIPLE_SINGLE,
        &TRIPLE_DOUBLE,
    ];

    #[test]
    fn all_byte_values_and_lanes_match_scalar() {
        for class in CLASSES {
            for value in 0..=255u8 {
                for offset in 0..96 {
                    let mut bytes = [b'a'; 128];
                    bytes[offset] = value;
                    assert_eq!(
                        next(&bytes, 0, bytes.len(), class),
                        portable(&bytes, 0, bytes.len(), class)
                    );
                }
            }
        }
    }

    #[test]
    fn range_boundaries_and_unaligned_inputs() {
        for class in CLASSES {
            for length in 0..100 {
                let mut bytes = vec![b'x'; length + 3];
                for (i, byte) in bytes.iter_mut().enumerate() {
                    if i % 17 == 0 {
                        *byte = b'\n';
                    }
                }
                let input = &bytes[3..];
                for start in 0..=length {
                    for end in [start, length, (start + 33).min(length)] {
                        assert_eq!(
                            next(input, start, end, class),
                            portable(input, start, end, class)
                        );
                    }
                }
            }
        }
    }
}
