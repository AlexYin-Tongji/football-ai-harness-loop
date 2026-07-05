from __future__ import annotations

from services.report_api.claim_ledger import (
    build_numeric_claim_ledger,
    sanitize_text_against_evidence,
)
from services.report_api.domain import ReportRequest
from services.report_api.validation import (
    ReportValidationError,
    validate_generated_report,
)


def base_request() -> ReportRequest:
    return ReportRequest.model_validate(
        {
            "report_type": "daily_football_digest",
            "subject": "今日球脉",
            "report_date": "2026-07-04",
            "data_cutoff": "2026-07-04T08:00:00Z",
            "evidence": [
                {
                    "id": "ev-match",
                    "title": "Portugal beat Croatia 2-1",
                    "url": "https://example.com/match",
                    "published_at": "2026-07-04T07:00:00Z",
                    "source_name": "Approved publisher",
                    "summary": (
                        "Portugal beat Croatia 2-1 after Ramos scored "
                        "in the 72nd-minute."
                    ),
                }
            ],
        }
    )


def valid_report() -> dict:
    return {
        "title": "今日球脉",
        "executive_summary": "葡萄牙与克罗地亚的比赛成为今日主线。",
        "sections": [
            {
                "heading": "葡萄牙险胜克罗地亚",
                "body": "葡萄牙2-1击败克罗地亚，Ramos在第72分钟破门。",
                "evidence_ids": ["ev-match"],
                "category": "match",
            }
        ],
        "warnings": [],
        "prediction": None,
    }


def test_claim_gate_accepts_numbers_present_in_cited_evidence() -> None:
    report = validate_generated_report(valid_report(), base_request())

    assert report.sections[0].heading == "葡萄牙险胜克罗地亚"


def test_claim_gate_rejects_unsupported_scoreline() -> None:
    output = valid_report()
    output["sections"][0]["body"] = "葡萄牙3-1击败克罗地亚，Ramos在第72分钟破门。"

    try:
        validate_generated_report(output, base_request())
    except ReportValidationError as exc:
        assert any("numeric claim" in error for error in exc.errors)
    else:
        raise AssertionError("unsupported numeric claim should fail")


def test_numeric_claim_ledger_and_sanitizer_remove_unsupported_stage_number() -> None:
    request = base_request()

    ledger = build_numeric_claim_ledger(request.evidence)
    body, changed = sanitize_text_against_evidence(
        "葡萄牙2-1击败克罗地亚，赛后被写成晋级16强。",
        request.evidence,
        ["ev-match"],
    )

    assert any("2-1" in item["numbers"] for item in ledger)
    assert "16" in changed
    assert "16" not in body
    assert "2-1" in body


def test_daily_gate_rejects_upcoming_fixture_written_as_today_match() -> None:
    request = ReportRequest.model_validate(
        {
            "report_type": "daily_football_digest",
            "subject": "今日球脉",
            "report_date": "2026-07-04",
            "data_cutoff": "2026-07-04T08:00:00Z",
            "evidence": [
                {
                    "id": "england-mexico-preview",
                    "title": (
                        "England get hostile welcome on arrival at Mexico City "
                        "hotel for World Cup showdown"
                    ),
                    "url": "https://www.theguardian.com/football/example",
                    "published_at": "2026-07-04T07:00:00Z",
                    "source_name": "The Guardian Football",
                    "summary": (
                        "England arrived at their team hotel ahead of the Mexico "
                        "match. Kick-off is scheduled for Sunday, 5 July."
                    ),
                }
            ],
        }
    )
    output = {
        "title": "今日球脉",
        "executive_summary": "英格兰和墨西哥的赛前安排成为今日足球新闻焦点。",
        "sections": [
            {
                "heading": "英格兰墨西哥城迎苦战",
                "body": "今日世界杯赛场，英格兰客场挑战墨西哥，比赛已经展开较量。",
                "evidence_ids": ["england-mexico-preview"],
                "category": "match",
            }
        ],
        "warnings": [],
        "prediction": None,
    }

    try:
        validate_generated_report(output, request)
    except ReportValidationError as exc:
        assert any("upcoming fixture" in error for error in exc.errors)
    else:
        raise AssertionError("upcoming fixture copy should fail the daily gate")


def test_daily_gate_rejects_placeholder_match_report_copy() -> None:
    request = ReportRequest.model_validate(
        {
            "report_type": "daily_football_digest",
            "subject": "今日球脉",
            "report_date": "2026-07-04",
            "data_cutoff": "2026-07-04T08:00:00Z",
            "evidence": [
                {
                    "id": "argentina-cape-verde",
                    "title": "Argentina beat Cape Verde 3-2 in World Cup thriller",
                    "url": "https://www.theguardian.com/football/example-2",
                    "published_at": "2026-07-04T07:00:00Z",
                    "source_name": "The Guardian Football",
                    "summary": "Argentina beat Cape Verde 3-2 in a World Cup match.",
                }
            ],
        }
    )
    output = {
        "title": "今日球脉",
        "executive_summary": "阿根廷和佛得角的比赛成为今日世界杯赛场焦点。",
        "sections": [
            {
                "heading": "阿根廷 3-2 佛得角",
                "body": (
                    "阿根廷以3-2战胜佛得角。目前尚无关于进球时间、进球方式或"
                    "关键事件的详细报道，比赛进程依然存疑。本段战报将在获取"
                    "更多信息后补充更新。"
                ),
                "evidence_ids": ["argentina-cape-verde"],
                "category": "match",
            }
        ],
        "warnings": [],
        "prediction": None,
    }

    try:
        validate_generated_report(output, request)
    except ReportValidationError as exc:
        assert any("placeholder" in error for error in exc.errors)
    else:
        raise AssertionError("placeholder-heavy match copy should fail")


def test_daily_gate_rejects_transfer_section_without_transfer_evidence() -> None:
    request = ReportRequest.model_validate(
        {
            "report_type": "daily_football_digest",
            "subject": "今日球脉",
            "report_date": "2026-07-05",
            "data_cutoff": "2026-07-05T08:00:00Z",
            "evidence": [
                {
                    "id": "aramco-worldcup",
                    "title": "Aramco sponsor faces World Cup pollution criticism",
                    "url": "https://example.com/aramco",
                    "published_at": "2026-07-05T07:00:00Z",
                    "source_name": "Approved publisher",
                    "summary": (
                        "The World Cup sponsor faces community criticism "
                        "near Houston."
                    ),
                }
            ],
        }
    )
    output = {
        "title": "今日球脉",
        "executive_summary": "今日转会窗没有可确认的新动态。",
        "sections": [
            {
                "heading": "今日转会窗平静",
                "body": "根据证据，今日无具体球员转会信息。",
                "evidence_ids": ["aramco-worldcup"],
                "category": "transfer",
            }
        ],
        "warnings": [],
        "prediction": None,
    }

    try:
        validate_generated_report(output, request)
    except ReportValidationError as exc:
        assert any("no transfer evidence" in error for error in exc.errors)
    else:
        raise AssertionError("transfer section without transfer evidence should fail")
