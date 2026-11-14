//! Pure numeric analysis shared by the native service and browser WASM.
pub mod account;
pub mod book;
pub mod cost;
pub mod equity;

pub const PROTOCOL: u32 = 2;
pub const ALGORITHM: &str = "local-analysis-1";

pub fn trade_value(
    price: Option<f64>,
    quantity: Option<f64>,
    multiplier: Option<f64>,
    reference: Option<f64>,
    side: &str,
) -> (Option<f64>, Option<f64>, Option<f64>) {
    let value = price
        .zip(quantity)
        .zip(multiplier)
        .map(|((p, q), m)| p * q * m);
    let difference = price
        .zip(reference)
        .filter(|_| matches!(side, "buy" | "sell"))
        .map(|(p, r)| (p - r) * if side == "buy" { 1.0 } else { -1.0 });
    (
        value,
        difference
            .zip(quantity)
            .zip(multiplier)
            .map(|((d, q), m)| d * q * m),
        difference.zip(reference).map(|(d, r)| d / r * 1e4),
    )
}
