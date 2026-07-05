from __future__ import annotations

from services.report_api.providers.base import LLMRequest, LLMResult


class MockProvider:
    """Deterministic, clearly labelled demo provider for local product work."""

    async def generate_json(self, request: LLMRequest) -> LLMResult:
        if request.purpose == "research_plan":
            return LLMResult(
                output={
                    "queries": [
                        {
                            "query": "FIFA World Cup football transfer news today",
                            "purpose": "match_news",
                            "sources": ["rss", "gdelt", "newsapi"],
                        },
                        {
                            "query": "World Cup match preview team news prediction",
                            "purpose": "prediction_context",
                            "sources": ["gdelt", "newsapi"],
                        },
                    ],
                    "min_items": 1,
                    "allow_discovery_only": True,
                },
                provider="mock",
                model=request.model,
            )
        if request.purpose == "evidence_refinement":
            candidate_ids = request.metadata.get("candidate_ids") or []
            return LLMResult(
                output={
                    "items": [
                        {
                            "source_evidence_id": evidence_id,
                            "title": "Mock 精简资料",
                            "concise_summary": (
                                "mock 模式下的资料精简结果；真实模式会压缩标题、"
                                "短摘录、时间和来源状态。"
                            ),
                            "key_points": ["保留来源边界", "不新增事实"],
                        }
                        for evidence_id in candidate_ids[:8]
                    ],
                    "warnings": ["当前为 mock 资料精简。"],
                },
                provider="mock",
                model=request.model,
            )
        if request.purpose == "enhancement_plan":
            return LLMResult(
                output={
                    "needs": [],
                    "warnings": ["mock 模式不自动调用外部增强工具。"],
                },
                provider="mock",
                model=request.model,
            )
        if request.purpose == "leader_column_plan":
            evidence_ids = request.metadata.get("evidence_ids") or []
            first = evidence_ids[0] if evidence_ids else "ev-1"
            return LLMResult(
                output={
                    "columns": [
                        {
                            "column_id": "match_report",
                            "title": "赛场主线",
                            "category": "match",
                            "specialist_group": "match_report",
                            "priority": 1,
                            "evidence_ids": [first],
                            "search_iterations": 2,
                            "enrichment_targets": [],
                            "media_targets": ["match report image"],
                            "instructions": "mock Leader 将赛事材料交给战报小组。",
                        }
                    ],
                    "warnings": ["当前为 mock Leader 栏目规划。"],
                },
                provider="mock",
                model=request.model,
            )
        report_type = request.metadata["report_type"]
        evidence_id = request.metadata["evidence_ids"][0]
        subject = request.metadata["subject"]

        if request.purpose.startswith("daily_research:"):
            desk = request.metadata["desk"]
            return LLMResult(
                output={
                    "desk": desk,
                    "key_items": [
                        {
                            "claim": "演示研究条目",
                            "evidence_ids": [evidence_id],
                        }
                    ],
                    "rumor_items": [],
                    "conflicts": [],
                    "unknowns": ["mock 模式没有实时研究资料"],
                },
                provider="mock",
                model=request.model,
            )

        if request.purpose.startswith("daily_desk_write:"):
            desk = request.metadata["desk"]
            return LLMResult(
                output={
                    "desk": desk,
                    "heading": "赛场脉搏" if desk == "match_news" else "转会雷达",
                    "summary": "这是经过独立研究桌整理的演示栏目。",
                    "sections": [
                        {
                            "heading": "今日要点",
                            "body": "正式模式会保留证据等级并区分事实与传闻。",
                            "evidence_ids": [evidence_id],
                        }
                    ],
                    "warnings": ["当前为 mock 演示。"],
                },
                provider="mock",
                model=request.model,
            )

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
            "daily_football_digest": {
                "summary": (
                    "这是一份今日球脉演示，将赛事动态与转会市场分桌研究，"
                    "再合并为一份有来源、有传闻等级的每日足球情报。"
                ),
                "sections": [
                    (
                        "赛场与赛事脉搏",
                        "整理赛果、赛程、球队动态与今日观赛重点。",
                    ),
                    (
                        "转会市场雷达",
                        "同时保留官宣、实质进展与明确标注的未核实传闻。",
                    ),
                ],
            },
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
                "analysis_process": [
                    {
                        "claim": "先读取证据包中的赛前信息，确认当前只是演示上下文。",
                        "evidence_ids": [evidence_id],
                    },
                    {
                        "claim": "再平衡支持因素和反方因素，所以概率保持低置信度。",
                        "evidence_ids": [evidence_id],
                    },
                    {
                        "claim": "最后把未知项保留在报告中，不给出确定比分。",
                        "evidence_ids": [evidence_id],
                    },
                ],
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
