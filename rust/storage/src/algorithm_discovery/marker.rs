//! Fixed-token block recognizer. No generic string search, regex, or allocation.
use super::MARKER;

const LOW: u64 = 0x0101_0101_0101_0101;
const HIGH: u64 = 0x8080_8080_8080_8080;
const LAST: usize = MARKER.len() - 1;
const SEPARATOR: usize = 9;
const _: () = assert!(MARKER[SEPARATOR] == b'_');

#[inline]
fn exact(bytes: &[u8], offset: usize) -> bool {
    bytes.get(offset..offset + MARKER.len()) == Some(MARKER)
}

#[inline]
fn zero_bytes(word: u64) -> u64 {
    // Borrow can add adjacent candidates; exact() rejects them. No true zero is lost.
    word.wrapping_sub(LOW) & !word & HIGH
}

pub(crate) fn portable(bytes: &[u8], start: usize) -> Option<usize> {
    let limit = bytes.len().checked_sub(MARKER.len())? + 1;
    let mut i = start;
    while i.checked_add(8).is_some_and(|end| end <= limit) {
        let first = u64::from_le_bytes(bytes[i..i + 8].try_into().unwrap());
        let last = u64::from_le_bytes(bytes[i + LAST..i + LAST + 8].try_into().unwrap());
        let mut mask = zero_bytes(first ^ (LOW * u64::from(b'@')))
            & zero_bytes(last ^ (LOW * u64::from(b'm')));
        if mask != 0 {
            let middle =
                u64::from_le_bytes(bytes[i + SEPARATOR..i + SEPARATOR + 8].try_into().unwrap());
            mask &= zero_bytes(middle ^ (LOW * u64::from(b'_')));
        }
        while mask != 0 {
            let offset = i + mask.trailing_zeros() as usize / 8;
            if exact(bytes, offset) {
                return Some(offset);
            }
            mask &= mask - 1;
        }
        i += 8;
    }
    while i < limit {
        if bytes[i] == b'@' && bytes[i + LAST] == b'm' && exact(bytes, i) {
            return Some(i);
        }
        i += 1;
    }
    None
}

#[cfg(target_arch = "x86_64")]
mod x86 {
    use super::{exact, portable, LAST, MARKER, SEPARATOR};
    use std::arch::x86_64::*;

    #[inline(always)]
    unsafe fn mask(pointer: *const u8, needle: __m256i) -> u32 {
        // SAFETY: callers establish AVX2 and at least 32 readable bytes.
        unsafe {
            _mm256_movemask_epi8(_mm256_cmpeq_epi8(
                _mm256_loadu_si256(pointer.cast()),
                needle,
            )) as u32
        }
    }

    #[inline(always)]
    unsafe fn verify(bytes: &[u8], base: usize, starts: u32, tail: __m256i) -> Option<usize> {
        if starts == 0 {
            return None;
        }
        // SAFETY: base+32 <= valid-start limit, so the shifted tail load is in bounds.
        let mut candidates = starts & unsafe { mask(bytes.as_ptr().add(base + LAST), tail) };
        if candidates != 0 {
            // SAFETY: the separator load is closer than the checked tail load.
            candidates &= unsafe {
                mask(
                    bytes.as_ptr().add(base + SEPARATOR),
                    _mm256_set1_epi8(b'_' as i8),
                )
            };
        }
        while candidates != 0 {
            let offset = base + candidates.trailing_zeros() as usize;
            if exact(bytes, offset) {
                return Some(offset);
            }
            candidates &= candidates - 1;
        }
        None
    }

    #[target_feature(enable = "avx2")]
    pub(super) unsafe fn next(bytes: &[u8], start: usize) -> Option<usize> {
        let limit = bytes.len().checked_sub(MARKER.len())? + 1;
        if start >= limit {
            return None;
        }
        let at = _mm256_set1_epi8(b'@' as i8);
        let tail = _mm256_set1_epi8(b'm' as i8);
        let mut i = start;
        while limit - i >= 128 {
            // SAFETY: the loop bounds cover all four loads and their shifted tails.
            let starts = unsafe {
                [
                    mask(bytes.as_ptr().add(i), at),
                    mask(bytes.as_ptr().add(i + 32), at),
                    mask(bytes.as_ptr().add(i + 64), at),
                    mask(bytes.as_ptr().add(i + 96), at),
                ]
            };
            if starts[0] | starts[1] | starts[2] | starts[3] != 0 {
                for (slot, bits) in starts.into_iter().enumerate() {
                    // SAFETY: each slot is wholly inside the same checked 128-start block.
                    if let Some(offset) = unsafe { verify(bytes, i + slot * 32, bits, tail) } {
                        return Some(offset);
                    }
                }
            }
            i += 128;
        }
        while limit - i >= 32 {
            // SAFETY: at least 32 complete marker start positions remain.
            if let Some(offset) = unsafe { verify(bytes, i, mask(bytes.as_ptr().add(i), at), tail) }
            {
                return Some(offset);
            }
            i += 32;
        }
        portable(bytes, i)
    }
}

pub fn backend() -> &'static str {
    #[cfg(target_arch = "x86_64")]
    if std::is_x86_feature_detected!("avx2") {
        return "avx2-128-byte-blocks";
    }
    "portable-8-byte-blocks"
}

pub fn next(bytes: &[u8], start: usize) -> Option<usize> {
    if bytes.len().saturating_sub(start) < MARKER.len() {
        return None;
    }
    #[cfg(target_arch = "x86_64")]
    if std::is_x86_feature_detected!("avx2") {
        // SAFETY: runtime detection guards the target-feature function; loads check slice bounds.
        return unsafe { x86::next(bytes, start) };
    }
    portable(bytes, start)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn check(bytes: &[u8], start: usize) {
        let expected = bytes
            .windows(MARKER.len())
            .enumerate()
            .skip(start)
            .find_map(|(i, value)| (value == MARKER).then_some(i));
        assert_eq!(
            portable(bytes, start),
            expected,
            "portable len={} start={start}",
            bytes.len()
        );
        assert_eq!(
            next(bytes, start),
            expected,
            "selected len={} start={start}",
            bytes.len()
        );
    }

    #[test]
    fn every_offset_alignment_and_tail() {
        for length in 0..280 {
            let mut bytes = vec![b'x'; length];
            check(&bytes, 0);
            for offset in 0..length.saturating_sub(MARKER.len() - 1) {
                bytes[offset..offset + MARKER.len()].copy_from_slice(MARKER);
                for start in [0, offset, offset + 1, length, length + 1] {
                    check(&bytes, start);
                }
                bytes[offset..offset + MARKER.len()].fill(b'x');
            }
        }
    }

    #[test]
    fn reject_each_single_byte_mutation() {
        for index in 0..MARKER.len() {
            for byte in 0..=255u8 {
                if byte == MARKER[index] {
                    continue;
                }
                let mut bytes = vec![b'@'; 320];
                bytes[129..129 + MARKER.len()].copy_from_slice(MARKER);
                bytes[129 + index] = byte;
                check(&bytes, 0);
            }
        }
    }

    #[test]
    fn arbitrary_bytes_and_multiple_matches() {
        let mut state = 0x20261110_u64;
        for length in [19, 31, 32, 33, 63, 64, 127, 128, 129, 256, 512, 4096] {
            let mut bytes = Vec::new();
            for _ in 0..length {
                state ^= state << 13;
                state ^= state >> 7;
                state ^= state << 17;
                bytes.push(state as u8);
            }
            for offset in [
                0,
                (length / 3).min(length - MARKER.len()),
                length - MARKER.len(),
            ] {
                bytes[offset..offset + MARKER.len()].copy_from_slice(MARKER);
            }
            for start in 0..=length {
                check(&bytes, start);
            }
        }
    }
}
