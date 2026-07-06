from __future__ import annotations

import re

from services.report_api.domain import ReportSection

BRIEF_LABELS = ("核心", "背景", "下一步", "边界")

DAILY_BRIEFING_PLAYBOOK = {
    "product_role": (
        "今日球脉是关键信息整合商：从大量证据里挑出今天最重要、最有变化、"
        "最值得用户继续跟进的足球信息，不创作剧情，也不穷尽罗列所有消息。"
    ),
    "section_shape": [
        "【核心】一句话说明发生了什么，只写证据支持的事实或线索。",
        "【背景】只补能帮助理解该主线的球队、球员、教练或赛事语境。",
        "【下一步】写读者应继续关注的比赛、体检、官宣、复核或赛程影响；证据没有则省略。",
        "【边界】说明证据缺口、传闻状态或结构化源未覆盖的字段；不要放正文占位句。",
    ],
    "selection_rules": [
        "优先选择当天发生变化的主线，而不是把所有来源都写进正文。",
        "比赛和转会是独立主线；同一 story_cluster_id 只出现一次。",
        "基础赛果、比分、开球时间来自结构化事实层；新闻只做解释和背景补充。",
        "未在证据中出现的进球分钟、金额、合同年限、履历数字和直接引语一律不补。",
    ],
    "domain_knowledge": {
        "transfer_stage_ladder": [
            "传闻/关注",
            "接触",
            "报价",
            "谈判",
            "原则协议",
            "体检",
            "签约/官宣",
            "辟谣/停止",
        ],
        "match_rules": [
            "report_date 与 time_scope 均按北京时间自然日判断。",
            "点球大战比分必须保留常规时间/点球/总结果的证据表达，不自行重算。",
            "只有 completed_match 证据能进入战报；preview、arrival、"
            "kickoff、hotel、schedule 只能进入场外或背景。",
            "结构化源没有 events 时，只写结果和影响，不写进球者或分钟时间线。",
        ],
        "people_context": [
            "人物/教练信息只服务主线理解：身份、位置、现俱乐部、执教/履历节点、为什么与今天相关。",
            "年龄、赛季数据、冠军数、转会费等数字必须由当前证据或授权结构化源支持。",
        ],
        "status_labels": {
            "corroborated": "已由当前证据包支持",
            "publisher_report": "来自发布方报道",
            "unverified_lead": "未核实线索，不能升级为确认事实",
        },
    },
    "forbidden_visible_text": [
        "同簇标题",
        "来源原摘",
        "精简提炼",
        "摘要信息",
        "结构化赛果｜",
        "结构化赛程｜",
    ],
}


def daily_briefing_playbook_payload() -> dict[str, object]:
    return DAILY_BRIEFING_PLAYBOOK


def ensure_sentence(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value.endswith(("。", "！", "？", ".", "!", "?")):
        return value
    return value + "。"


def format_key_brief_body(
    *,
    core: str,
    background: str = "",
    next_step: str = "",
    boundary: str = "",
) -> str:
    fields = [
        ("核心", core),
        ("背景", background),
        ("下一步", next_step),
        ("边界", boundary),
    ]
    return " ".join(
        f"【{label}】{ensure_sentence(text)}"
        for label, text in fields
        if ensure_sentence(text)
    )


def strip_brief_labels(value: str) -> str:
    return re.sub(r"【(?:核心|背景|下一步|边界)】", "", value).strip()


def executive_summary_from_sections(
    sections: list[ReportSection], *, fallback: str
) -> str:
    summaries: list[str] = []
    for section in sections[:4]:
        match = re.search(
            r"【核心】(.+?)(?:【背景】|【下一步】|【边界】|$)", section.body
        )
        text = match.group(1).strip() if match else strip_brief_labels(section.body)
        if text:
            summaries.append(ensure_sentence(text))
    if summaries:
        return " ".join(summaries)[:1200]
    return fallback
