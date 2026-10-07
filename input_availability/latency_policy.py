"""Input readiness budgets: product delivery timeliness minus one day.
User-supplied delivery requirements, 2026-10-07; not provider SLAs.
This policy changes thresholds only, not the measurement of latency.
"""
DELIVERY_TIMELINESS_DAYS = {
    '01': 8, '02': 4, '03': 2, '04': 2, '05': 4, '06': 2,
    '07': 9, '08': 9, '09': 4, '10': 8, '11': 4,
}
INPUT_LATENCY_DAYS = {p: days - 1 for p, days in DELIVERY_TIMELINESS_DAYS.items()}

def expected_latency_days(product: str) -> int | None:
    """Shared inputs use the strictest delivery budget of their products."""
    codes = [code.strip().removeprefix('OU-S5-02-') for code in product.split('/')]
    budgets = [INPUT_LATENCY_DAYS[code] for code in codes if code in INPUT_LATENCY_DAYS]
    return min(budgets) if budgets else None
