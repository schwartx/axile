"""诊断采样有界且不允许读取者篡改时间证据。"""

from axile.executor.quote_diagnostics import QuoteDiagnostics


def test_samples_keep_rejection_reason_and_bounded_frames():
    samples = QuoteDiagnostics(max_symbols=2, frames=2)
    for index, reason in enumerate((None, "stale_exchange_time", "out_of_order")):
        samples.observe("rb", event_time_ms=index * 1000, received_at=10 + index, reason=reason)
    report = samples.snapshot("rb")
    assert (report["received"], report["accepted"], report["rejected"]) == (3, 1, 2)
    assert [row["reason"] for row in report["frames"]] == ["stale_exchange_time", "out_of_order"]
    report["frames"][0]["reason"] = "changed"
    assert samples.snapshot("rb")["frames"][0]["reason"] == "stale_exchange_time"
    for symbol in ("ag", "cu"):
        samples.observe(symbol, event_time_ms=1, received_at=1, reason=None)
    assert samples.snapshot("rb")["sampled"] is False
