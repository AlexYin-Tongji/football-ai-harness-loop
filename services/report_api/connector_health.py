from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

HealthStatus = Literal[
    "not_configured",
    "healthy",
    "degraded",
    "needs_attention",
    "unknown",
]
LeagueCoverageStatus = Literal[
    "not_configured",
    "covered",
    "not_covered",
    "unauthorized",
    "rate_limited",
    "error",
]


@dataclass(frozen=True)
class LeagueProbe:
    name: str
    country: str
    queries: tuple[str, ...]


BIG_FIVE_LEAGUES: tuple[LeagueProbe, ...] = (
    LeagueProbe("Premier League", "England", ("Premier League",)),
    LeagueProbe("La Liga", "Spain", ("La Liga", "LaLiga", "Primera División")),
    LeagueProbe("Serie A", "Italy", ("Serie A",)),
    LeagueProbe("Bundesliga", "Germany", ("Bundesliga",)),
    LeagueProbe("Ligue 1", "France", ("Ligue 1",)),
)


class LeagueCoverage(BaseModel):
    name: str
    country: str
    status: LeagueCoverageStatus
    message: str
    matched_league_ids: list[int] = Field(default_factory=list, max_length=8)


class ConnectorHealthItem(BaseModel):
    connector_id: str
    configured: bool
    status: HealthStatus
    message: str


class SportmonksHealth(ConnectorHealthItem):
    big_five_leagues: list[LeagueCoverage] = Field(default_factory=list)


class MediaHealth(BaseModel):
    commons: ConnectorHealthItem
    youtube: ConnectorHealthItem
    visual_relevance: ConnectorHealthItem


class ConnectorHealthResponse(BaseModel):
    generated_at: datetime
    overall_status: HealthStatus
    sportmonks: SportmonksHealth
    football_data: ConnectorHealthItem
    news_api: ConnectorHealthItem
    media: MediaHealth
    recommendations: list[str] = Field(default_factory=list, max_length=12)


def _token() -> str | None:
    return os.getenv("SPORTMONKS_API_TOKEN") or None


def _news_api_key() -> str | None:
    return os.getenv("NEWS_API_KEY") or None


def _football_data_key() -> str | None:
    return os.getenv("FOOTBALL_DATA_API_KEY") or None


def _google_vision_configured() -> bool:
    return bool(
        os.getenv("GOOGLE_CLOUD_VISION_API_KEY")
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    )


def _league_ids(payload: dict) -> list[int]:
    ids: list[int] = []
    for item in payload.get("data", []):
        league_id = item.get("id")
        if isinstance(league_id, int):
            ids.append(league_id)
    return ids[:8]


async def _probe_sportmonks_league(
    client: httpx.AsyncClient, token: str, probe: LeagueProbe
) -> LeagueCoverage:
    for query in probe.queries:
        try:
            response = await client.get(
                "https://api.sportmonks.com/v3/football/leagues/search/" + quote(query),
                headers={"Accept": "application/json", "Authorization": token},
            )
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            return LeagueCoverage(
                name=probe.name,
                country=probe.country,
                status="error",
                message=f"Sportmonks 请求失败：{exc.__class__.__name__}",
            )

        if response.status_code in {401, 403}:
            return LeagueCoverage(
                name=probe.name,
                country=probe.country,
                status="unauthorized",
                message="当前 Sportmonks token 无效、过期或无权访问足球 API。",
            )
        if response.status_code == 429:
            return LeagueCoverage(
                name=probe.name,
                country=probe.country,
                status="rate_limited",
                message="Sportmonks 正在限流，稍后再探测。",
            )
        if response.status_code >= 400:
            return LeagueCoverage(
                name=probe.name,
                country=probe.country,
                status="error",
                message=f"Sportmonks 返回 HTTP {response.status_code}。",
            )

        payload = response.json()
        ids = _league_ids(payload)
        if ids:
            return LeagueCoverage(
                name=probe.name,
                country=probe.country,
                status="covered",
                message="当前 token 至少能检索到该联赛元数据。",
                matched_league_ids=ids,
            )

    return LeagueCoverage(
        name=probe.name,
        country=probe.country,
        status="not_covered",
        message=("当前 token 未返回该联赛；常见原因是套餐未选择该联赛或免费层不包含。"),
    )


async def probe_sportmonks_big_five(
    transport: httpx.AsyncBaseTransport | None = None,
) -> SportmonksHealth:
    token = _token()
    if not token:
        return SportmonksHealth(
            connector_id="sportmonks",
            configured=False,
            status="not_configured",
            message="未配置 SPORTMONKS_API_TOKEN。",
            big_five_leagues=[
                LeagueCoverage(
                    name=item.name,
                    country=item.country,
                    status="not_configured",
                    message="未配置 Sportmonks token，无法探测覆盖。",
                )
                for item in BIG_FIVE_LEAGUES
            ],
        )

    timeout = httpx.Timeout(18.0, connect=6.0)
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, transport=transport
    ) as client:
        coverage = [
            await _probe_sportmonks_league(client, token, probe)
            for probe in BIG_FIVE_LEAGUES
        ]

    statuses = {item.status for item in coverage}
    if "unauthorized" in statuses:
        status: HealthStatus = "needs_attention"
        message = "Sportmonks token 无法授权足球联赛探测。"
    elif "covered" in statuses and statuses <= {"covered"}:
        status = "healthy"
        message = "五大联赛均可被当前 Sportmonks token 检索。"
    elif "covered" in statuses:
        status = "degraded"
        message = "Sportmonks 已配置，但五大联赛覆盖不完整。"
    elif "rate_limited" in statuses:
        status = "degraded"
        message = "Sportmonks 已配置，但探测受到限流。"
    else:
        status = "needs_attention"
        message = "Sportmonks 已配置，但五大联赛均未返回可用数据。"

    return SportmonksHealth(
        connector_id="sportmonks",
        configured=True,
        status=status,
        message=message,
        big_five_leagues=coverage,
    )


async def probe_news_api(
    transport: httpx.AsyncBaseTransport | None = None,
) -> ConnectorHealthItem:
    api_key = _news_api_key()
    if not api_key:
        return ConnectorHealthItem(
            connector_id="newsapi",
            configured=False,
            status="not_configured",
            message="未配置 NEWS_API_KEY；系统仍会使用 RSS 与 GDELT。",
        )
    try:
        timeout = httpx.Timeout(12.0, connect=5.0)
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, transport=transport
        ) as client:
            response = await client.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": "football",
                    "searchIn": "title,description",
                    "language": "en",
                    "pageSize": 1,
                },
                headers={"X-Api-Key": api_key},
            )
    except (httpx.TimeoutException, httpx.HTTPError) as exc:
        return ConnectorHealthItem(
            connector_id="newsapi",
            configured=True,
            status="degraded",
            message=f"NewsAPI 探测失败：{exc.__class__.__name__}。",
        )
    if response.status_code in {401, 403}:
        return ConnectorHealthItem(
            connector_id="newsapi",
            configured=True,
            status="needs_attention",
            message="NewsAPI Key 无效、过期或当前套餐无权访问 everything endpoint。",
        )
    if response.status_code == 429:
        return ConnectorHealthItem(
            connector_id="newsapi",
            configured=True,
            status="degraded",
            message="NewsAPI 正在限流；新闻发现会退回 RSS/GDELT。",
        )
    if response.status_code >= 400:
        return ConnectorHealthItem(
            connector_id="newsapi",
            configured=True,
            status="degraded",
            message=f"NewsAPI 返回 HTTP {response.status_code}。",
        )
    return ConnectorHealthItem(
        connector_id="newsapi",
        configured=True,
        status="healthy",
        message="NewsAPI 已连接，可作为新闻线索扩展层。",
    )


def local_connector_health_shell() -> tuple[
    ConnectorHealthItem,
    ConnectorHealthItem,
    ConnectorHealthItem,
    ConnectorHealthItem,
]:
    football_data = ConnectorHealthItem(
        connector_id="football-data-org",
        configured=bool(_football_data_key()),
        status="unknown" if _football_data_key() else "not_configured",
        message=(
            "已配置 FOOTBALL_DATA_API_KEY；比赛请求时会做真实读取。"
            if _football_data_key()
            else "未配置 FOOTBALL_DATA_API_KEY；预测基线会缺少该备用结构化来源。"
        ),
    )
    commons = ConnectorHealthItem(
        connector_id="wikimedia-commons-api",
        configured=True,
        status="healthy",
        message="Commons 无需密钥；报告只保存许可、署名、缩略图和文件页。",
    )
    youtube = ConnectorHealthItem(
        connector_id="youtube-data-api",
        configured=bool(os.getenv("YOUTUBE_API_KEY")),
        status=(
            "healthy"
            if os.getenv("YOUTUBE_API_KEY")
            and os.getenv("YOUTUBE_OFFICIAL_CHANNEL_IDS")
            else "degraded"
            if os.getenv("YOUTUBE_API_KEY")
            else "not_configured"
        ),
        message=(
            "YouTube Key 与官方频道白名单均已配置。"
            if os.getenv("YOUTUBE_API_KEY")
            and os.getenv("YOUTUBE_OFFICIAL_CHANNEL_IDS")
            else "已配置 YouTube Key，但缺少官方频道白名单。"
            if os.getenv("YOUTUBE_API_KEY")
            else "未配置 YouTube Key；报告不会附带官方视频。"
        ),
    )
    visual = ConnectorHealthItem(
        connector_id="google-cloud-vision",
        configured=_google_vision_configured(),
        status="unknown" if _google_vision_configured() else "not_configured",
        message=(
            "已配置 Google Vision 凭据；可用于后续自动视觉相关性核验。"
            if _google_vision_configured()
            else "未配置视觉识别服务；Commons 图片仅做许可证与元数据相关性筛选。"
        ),
    )
    return football_data, commons, youtube, visual


async def collect_connector_health(
    *,
    sportmonks_transport: httpx.AsyncBaseTransport | None = None,
    news_transport: httpx.AsyncBaseTransport | None = None,
) -> ConnectorHealthResponse:
    sportmonks = await probe_sportmonks_big_five(sportmonks_transport)
    news_api = await probe_news_api(news_transport)
    football_data, commons, youtube, visual = local_connector_health_shell()
    recommendations: list[str] = []

    if sportmonks.status in {"needs_attention", "degraded"}:
        recommendations.append(
            "在 Sportmonks 后台确认是否已选择 Premier League、La Liga、Serie A、"
            "Bundesliga、Ligue 1；免费层通常不足以覆盖五大联赛。"
        )
    if news_api.status in {"not_configured", "degraded"}:
        recommendations.append(
            "NewsAPI 只作为线索扩展层；若需要更强事件聚类，可评估 Event Registry。"
        )
    if youtube.status != "healthy":
        recommendations.append(
            "视频进入报告前必须配置 YouTube Key 和人工确认的官方频道 ID。"
        )
    if visual.status == "not_configured":
        recommendations.append(
            "可优先申请 Google Cloud Vision 免费额度，用于图片标签/OCR/安全识别；"
            "球员身份仍建议保留人工复核。"
        )

    statuses = {
        sportmonks.status,
        news_api.status,
        football_data.status,
        youtube.status,
        visual.status,
    }
    if "needs_attention" in statuses:
        overall: HealthStatus = "needs_attention"
    elif "degraded" in statuses:
        overall = "degraded"
    elif statuses <= {"healthy", "unknown"}:
        overall = "healthy"
    else:
        overall = "degraded"

    return ConnectorHealthResponse(
        generated_at=datetime.now(UTC),
        overall_status=overall,
        sportmonks=sportmonks,
        football_data=football_data,
        news_api=news_api,
        media=MediaHealth(
            commons=commons,
            youtube=youtube,
            visual_relevance=visual,
        ),
        recommendations=recommendations,
    )
