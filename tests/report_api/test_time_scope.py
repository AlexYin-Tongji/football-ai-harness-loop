from __future__ import annotations

from datetime import UTC, date, datetime

from services.report_api.time_scope import build_report_time_scope, window_query_dates


def test_report_time_scope_uses_beijing_natural_day() -> None:
    scope = build_report_time_scope(
        date(2026, 7, 3),
        now=datetime(2026, 7, 5, 12, tzinfo=UTC),
    )

    assert scope.window_start_utc == datetime(2026, 7, 2, 16, tzinfo=UTC)
    assert scope.window_end_utc == datetime(2026, 7, 3, 16, tzinfo=UTC)
    assert scope.data_cutoff_utc == scope.window_end_utc
    assert scope.local_window_label == "北京时间 2026-07-03 00:00-24:00"
    assert window_query_dates(scope) == ("2026-07-02", "2026-07-03")


def test_report_time_scope_caps_current_day_at_now() -> None:
    scope = build_report_time_scope(
        date(2026, 7, 3),
        now=datetime(2026, 7, 3, 8, tzinfo=UTC),
    )

    assert scope.window_start_utc == datetime(2026, 7, 2, 16, tzinfo=UTC)
    assert scope.window_end_utc == datetime(2026, 7, 3, 16, tzinfo=UTC)
    assert scope.data_cutoff_utc == datetime(2026, 7, 3, 8, tzinfo=UTC)
