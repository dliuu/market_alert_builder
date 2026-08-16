"""Chinese market constants."""

# Chinese market identifier.
CN_MARKET = "CN"

# CSI 300 exposure. The index itself (000300.SS) is not servable on Tiingo's
# free tier — every candidate ticker format 404s (tiingo-cn-probe, 2026-08-16
# live run) — so the ETF proxy is engaged instead. Tracking error vs the true
# index is a documented caveat, not a blocker (cn/docs/open-questions.md CN-Q5).
CN_BENCHMARK = "510300.SS"
