from __future__ import annotations

from services.report_api.providers.base import LLMRequest, LLMResult


class MockProvider:
    """Deterministic, clearly labelled demo provider for local product work."""

    async def generate_json(self, request: LLMRequest) -> LLMResult:
        report_type = request.metadata["report_type"]
        evidence_id = request.metadata["evidence_ids"][0]
        subject = request.metadata["subject"]

        if request.purpose.startswith("prediction_opinion:"):
            role = request.metadata["opinion_role"]
            return LLMResult(
                output={
                    "role": role,
                    "home_win": 0.4,
                    "draw": 0.3,
                    "away_win": 0.3,
                    "key_claims": [
                        {
                            "claim": "演示分析席位，仅用于验证流程",
                            "evidence_ids": [evidence_id],
                        }
                    ],
                    "unknowns": ["mock 模式没有真实球队上下文"],
                    "confidence": "low",
                },
                provider="mock",
                model=request.model,
            )

        templates = {
            "world_cup_daily": {
                "summary": (
                    "这是一份世界杯日报演示。正式版本将把官方赛果、晋级形势与"
                    "当天新闻整理为一条清晰叙事，并让每个事实都能回到来源。"
                ),
                "sections": [
                    (
                        "30 秒摘要",
                        "日报优先回答三个问题：昨天发生了什么、对晋级路径有何影响、"
                        "今天最值得关注哪几场比赛。当前内容为本地演示。",
                    ),
                    (
                        "今日观察",
                        "正式数据接入后，这里将按北京时间列出赛程，并结合阵容、"
                        "休息时间和球队消息给出观看重点。",
                    ),
                ],
            },
            "transfer_daily": {
                "summary": (
                    "这是一份转会情报演示。系统会把重复转载压缩成事件时间线，"
                    "并区分传闻、接触、报价、协议、体检、官宣与辟谣。"
                ),
                "sections": [
                    (
                        "今日实质进展",
                        "正式版本只保留状态发生变化的消息，不把相同传闻的重复转载"
                        "包装成多条进展。",
                    ),
                    (
                        "可信度与冲突",
                        "每个事件都会展示独立来源数量、转载关系、冲突来源和信息截止时间。",
                    ),
                ],
            },
            "match_prediction": {
                "summary": (
                    "这是一份比赛预测演示。概率用于表达不确定性，不是确定赛果或"
                    "投注建议；正式版本会同时展示支持因素、反方证据和未知项。"
                ),
                "sections": [
                    (
                        "比赛判断",
                        "AI 会基于截止时间前的球队实力、近期表现、人员可用性、休息"
                        "与战术资料形成判断，并明确数据缺口。",
                    ),
                    (
                        "风险提示",
                        "首发变化、样本不足和淘汰赛偶然性都可能改变判断；开赛后"
                        "赛前版本将被冻结。",
                    ),
                ],
            },
        }
        template = templates[report_type]
        output: dict[str, object] = {
            "title": f"{subject}｜AI 足球报告",
            "executive_summary": template["summary"],
            "sections": [
                {
                    "heading": heading,
                    "body": body,
                    "evidence_ids": [evidence_id],
                }
                for heading, body in template["sections"]
            ],
            "warnings": [
                "当前使用本地 mock 数据，仅用于验证页面和工作流，不代表真实事实。"
            ],
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
                    {"claim": "演示支持因素", "evidence_ids": [evidence_id]}
                ],
                "counter_factors": [
                    {"claim": "演示反方因素", "evidence_ids": [evidence_id]}
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
