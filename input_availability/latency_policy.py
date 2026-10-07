"""Input readiness budgets: product delivery timeliness minus one day.
User-supplied delivery requirements, 2026-10-07; not provider SLAs.
This policy changes thresholds only, not the measurement of latency.
"""
import calendar
import datetime as dt

DELIVERY_TIMELINESS_DAYS = {
    '01': 8, '02': 4, '03': 2, '04': 2, '05': 4, '06': 2,
    '07': 9, '08': 9, '09': 4, '10': 8, '11': 4,
}
INPUT_LATENCY_DAYS = {p: days - 1 for p, days in DELIVERY_TIMELINESS_DAYS.items()}

def expected_latency_days(product: str, reference_date: dt.date | None = None) -> int | None:
    """Shared inputs use the strictest delivery budget of their products."""
    codes = [code.strip().removeprefix('OU-S5-02-') for code in product.split('/')]
    reference_date = reference_date or dt.date.today()
    year = reference_date.year + (reference_date.month == 12)
    month = reference_date.month % 12 + 1
    next_month = dt.date(year, month, min(reference_date.day, calendar.monthrange(year, month)[1]))
    month_days = (next_month - reference_date).days
    budgets = [INPUT_LATENCY_DAYS[code] + (month_days if code in {'07', '08'} else 0)
               for code in codes if code in INPUT_LATENCY_DAYS]
    return min(budgets) if budgets else None


def is_monthly_product(product: str) -> bool:
    codes = {c.strip().removeprefix('OU-S5-02-') for c in product.split('/')}
    return bool(codes) and codes <= {'07', '08'}
