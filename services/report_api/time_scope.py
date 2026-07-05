from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone

from services.report_api.domain import (
    ConsumerReportRequest,
    ReportRequest,
    ReportTimeScope,
)

BEIJING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")


def build_report_time_scope(
    report_date: date, *, now: datetime | None = None
) -> ReportTimeScope:
    now_utc = (now or datetime.now(UTC)).astimezone(UTC)
    local_start = datetime.combine(report_date, time.min, tzinfo=BEIJING_TZ)
    local_end = local_start + timedelta(days=1)
    window_start_utc = local_start.astimezone(UTC)
    window_end_utc = local_end.astimezone(UTC)
    data_cutoff_utc = min(window_end_utc, now_utc)
    return ReportTimeScope(
        report_date=report_date,
        window_start_utc=window_start_utc,
        window_end_utc=window_end_utc,
        data_cutoff_utc=data_cutoff_utc,
        local_window_label=f"北京时间 {report_date.isoformat()} 00:00-24:00",
    )


def scope_for_request(
    request: ConsumerReportRequest | ReportRequest, *, now: datetime | None = None
) -> ReportTimeScope:
    if request.time_scope is not None:
        return request.time_scope
    return build_report_time_scope(request.report_date, now=now)


def format_beijing(value: datetime) -> str:
    return value.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")


def window_query_dates(scope: ReportTimeScope) -> tuple[str, str]:
    start = scope.window_start_utc.date()
    end = (scope.window_end_utc - timedelta(seconds=1)).date()
    return start.isoformat(), end.isoformat()
