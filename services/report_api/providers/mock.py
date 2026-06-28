from __future__ import annotations

from services.report_api.providers.base import LLMRequest, LLMResult


class MockProvider:
    """Deterministic local provider for development and CI."""

    async def generate_json(self, request: LLMRequest) -> LLMResult:
        report_type = request.metadata["report_type"]
        evidence_ids = request.metadata["evidence_ids"]
        subject = request.metadata["subject"]

        output: dict[str, object] = {
            "title": f"{subject}｜AI 足球报告（演示）",
            "executive_summary": "这是 mock 模式生成的可验证报告，用于本地开发。",
            "sections": [
                {
                    "heading": "核心信息",
                    "body": (
                        "报告内容仅依据请求中提供的资料，正式环境将调用 DeepSeek V4。"
                    ),
                    "evidence_ids": [evidence_ids[0]],
                }
            ],
            "warnings": ["当前使用 mock provider，不代表真实比赛或转会判断。"],
            "prediction": None,
        }

        if report_type == "match_prediction":
            output["prediction"] = {
                "home_win": 0.4,
                "draw": 0.3,
                "away_win": 0.3,
                "qualification": (
                    {"home": 0.55, "away": 0.45}
                    if request.metadata.get("match_stage") == "knockout"
                    else None
                ),
                "scorelines": ["1-0", "1-1", "0-1"],
                "supporting_factors": [
                    {"claim": "演示支持因素", "evidence_ids": [evidence_ids[0]]}
                ],
                "counter_factors": [
                    {"claim": "演示反方因素", "evidence_ids": [evidence_ids[0]]}
                ],
                "unknowns": ["mock 模式没有真实球队上下文"],
                "confidence": "low",
            }

        return LLMResult(
            output=output,
            provider="mock",
            model=request.model,
            input_tokens=0,
            output_tokens=0,
            request_id="mock-request",
        )
