from __future__ import annotations

from services.report_api.evals import evaluate_daily_digest_result


def sample_result() -> dict:
    return {
        "run": {
            "steps": [
                {"name": "research_url_collection"},
                {"name": "research_evidence_refinement"},
                {"name": "research_leader_review"},
                {"name": "research_column_team_loop"},
                {"name": "research_writing_handoff"},
                {"name": "generate"},
                {"name": "quality_gate"},
            ]
        },
        "evidence": [
            {
                "id": "ev-match",
                "title": "Portugal beat Croatia 2-1",
                "summary": "Portugal scored a late goal.",
                "verification_status": "publisher_report",
            },
            {
                "id": "ev-transfer",
                "title": "Spurs transfer bid",
                "summary": "Tottenham made a transfer bid.",
                "verification_status": "unverified_lead",
            },
        ],
        "report": {
            "report": {
                "title": "今日球脉",
                "executive_summary": "葡萄牙比赛和热刺转会线索构成今日主线。",
                "sections": [
                    {
                        "heading": "葡萄牙险胜",
                        "body": "葡萄牙击败克罗地亚。",
                        "evidence_ids": ["ev-match"],
                        "category": "match",
                    },
                    {
                        "heading": "热刺转会线索",
                        "body": "据报道，热刺仍有一条未核实转会线索。",
                        "evidence_ids": ["ev-transfer"],
                        "category": "transfer",
                    },
                ],
                "enrichment": {
                    "media_assets": [
                        {
                            "title": "Portugal highlights",
                            "url": "https://example.com/video",
                            "evidence_ids": ["ev-match"],
                        }
                    ]
                },
            }
        },
    }


def test_daily_digest_eval_passes_complete_payload() -> None:
    report = evaluate_daily_digest_result(sample_result())

    assert report.passed
    assert report.score == 1


def test_daily_digest_eval_fails_missing_rumor_label() -> None:
    payload = sample_result()
    payload["report"]["report"]["sections"][1]["body"] = "热刺已经完成转会。"

    report = evaluate_daily_digest_result(payload)

    assert not report.passed
    assert any(item.check == "rumor.per_section_label" for item in report.findings)


def test_daily_digest_eval_fails_missing_minute_timeline() -> None:
    payload = sample_result()
    payload["evidence"][0]["summary"] = (
        "Portugal scored a 72nd-minute goal to beat Croatia 2-1."
    )

    report = evaluate_daily_digest_result(payload)

    assert not report.passed
    assert any(
        item.check == "match_timeline.minute_event_coverage"
        and not item.passed
        for item in report.findings
    )
