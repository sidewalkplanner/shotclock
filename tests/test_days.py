from datetime import date

from conftest import dt

from shotclock.days import DayBasis, add_days, count_days, covered_dates


def test_arrival_day_not_counted_departure_day_counted():
    assert covered_dates([(dt("2026-09-02T09:14"), dt("2026-09-04T10:00"))]) == {
        date(2026, 9, 3),
        date(2026, 9, 4),
    }


def test_same_day_interval_counts_nothing():
    assert covered_dates([(dt("2026-09-02T09:00"), dt("2026-09-02T17:00"))]) == set()


def test_back_to_back_intervals_do_not_double_count():
    dates = covered_dates(
        [
            (dt("2026-09-01T09:00"), dt("2026-09-03T09:00")),
            (dt("2026-09-03T15:00"), dt("2026-09-05T09:00")),
        ]
    )
    assert len(dates) == 4


def test_business_days_skip_weekends_and_state_holidays():
    # Wed 2026-11-25 .. Mon 2026-11-30: Thanksgiving and the Friday after are FL holidays.
    dates = covered_dates([(dt("2026-11-24T12:00"), dt("2026-11-30T12:00"))])
    assert count_days(dates, DayBasis.BUSINESS, "FL") == 2  # Wed 25, Mon 30
    assert count_days(dates, DayBasis.CALENDAR, "FL") == 6


def test_add_business_days_across_weekend():
    assert add_days(date(2026, 10, 30), 1, DayBasis.BUSINESS, "GA") == date(2026, 11, 2)
    assert add_days(date(2026, 10, 30), 0, DayBasis.BUSINESS, "GA") == date(2026, 10, 30)
