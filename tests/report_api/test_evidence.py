from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

import httpx

from services.report_api.article_reader import (
    extract_article_text,
    read_article_excerpt,
)
from services.report_api.domain import ConsumerReportRequest, Evidence
from services.report_api.evidence import (
    _annotate_story_clusters,
    _approved_newsapi_domains,
    _cluster_evidence,
    finish_evidence_selection,
    parse_bbc_feed,
    parse_gdelt_articles,
    parse_guardian_feed,
    parse_guardian_search_payload,
    parse_newsapi_articles,
)

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item><title>World Cup knockout preview</title>
    <link>https://www.theguardian.com/football/one</link>
    <description><![CDATA[Team news and a tactical preview.]]></description>
    <pubDate>Tue, 30 Jun 2026 08:00:00 GMT</pubDate></item>
  <item><title>World Cup injury update</title>
    <link>https://www.theguardian.com/football/two</link>
    <description><![CDATA[An important player faces a fitness test.]]></description>
    <pubDate>Tue, 30 Jun 2026 07:00:00 GMT</pubDate></item>
  <item><title>World Cup untrusted link</title>
    <link>https://example.com/not-approved</link>
    <description>Must not enter evidence.</description>
    <pubDate>Tue, 30 Jun 2026 06:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_guardian_feed_becomes_cutoff_bound_evidence() -> None:
    request = ConsumerReportRequest(
        report_type="world_cup_daily",
        subject="FIFA World Cup 2026 daily",
        report_date=date(2026, 6, 30),
    )

    evidence = parse_guardian_feed(
        FEED,
        request,
        cutoff=datetime(2026, 6, 30, 9, tzinfo=UTC),
    )

    assert len(evidence) == 2
    assert evidence[0].source_name == "The Guardian Football"
    assert str(evidence[0].url).startswith("https://www.theguardian.com/")
    assert evidence[0].id.startswith("guardian-")
    assert evidence[0].verification_status == "publisher_report"


def test_guardian_feed_respects_beijing_report_day_window() -> None:
    request = ConsumerReportRequest(
        report_type="world_cup_daily",
        subject="FIFA World Cup 2026 daily",
        report_date=date(2026, 7, 3),
    )
    feed = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item><title>World Cup early item outside Beijing day</title>
        <link>https://www.theguardian.com/football/early</link>
        <description>Published before the Beijing day starts.</description>
        <pubDate>Thu, 02 Jul 2026 15:59:00 GMT</pubDate></item>
      <item><title>World Cup match report inside Beijing day</title>
        <link>https://www.theguardian.com/football/inside</link>
        <description>Published within the requested Beijing report day.</description>
        <pubDate>Thu, 02 Jul 2026 18:00:00 GMT</pubDate></item>
      <item><title>World Cup late item outside Beijing day</title>
        <link>https://www.theguardian.com/football/late</link>
        <description>Published after the requested Beijing report day.</description>
        <pubDate>Sat, 04 Jul 2026 23:30:00 GMT</pubDate></item>
    </channel></rss>"""

    evidence = parse_guardian_feed(
        feed,
        request,
        cutoff=datetime(2026, 7, 3, 16, tzinfo=UTC),
        start_at=datetime(2026, 7, 2, 16, tzinfo=UTC),
    )

    assert len(evidence) == 1
    assert evidence[0].title == "World Cup match report inside Beijing day"
    assert "Beijing report day" in evidence[0].summary


def test_guardian_search_payload_keeps_query_matched_metadata() -> None:
    request = ConsumerReportRequest(
        report_type="daily_football_digest",
        subject="Tottenham transfer news",
        report_date=date(2026, 7, 3),
        focus=["热刺"],
    )
    payload = {
        "response": {
            "results": [
                {
                    "webTitle": (
                        "Transfer roundup: Everton clinch Hackney signing, "
                        "Spurs announce Fernandes"
                    ),
                    "webUrl": (
                        "https://www.theguardian.com/football/2026/jul/02/"
                        "transfer-latest-premier-league-everton-tottenham"
                    ),
                    "webPublicationDate": "2026-07-02T18:00:00Z",
                    "fields": {
                        "trailText": (
                            "Tottenham have signed Mateus Fernandes from West Ham."
                        )
                    },
                },
                {
                    "webTitle": "Unapproved mirror",
                    "webUrl": "https://example.com/football/spurs",
                    "webPublicationDate": "2026-07-02T18:00:00Z",
                    "fields": {"trailText": "Should not enter evidence."},
                },
            ]
        }
    }

    evidence = parse_guardian_search_payload(
        payload,
        request,
        cutoff=datetime(2026, 7, 3, 9, tzinfo=UTC),
    )

    assert len(evidence) == 1
    assert evidence[0].source_id == "guardian-open-platform"
    assert evidence[0].verification_status == "publisher_report"
    assert "Spurs announce Fernandes" in evidence[0].title


def test_gdelt_items_are_allowlisted_and_marked_as_unverified_leads() -> None:
    request = ConsumerReportRequest(
        report_type="transfer_daily",
        subject="Manchester United transfer news",
        report_date=date(2026, 6, 30),
    )
    payload = {
        "articles": [
            {
                "title": "Club linked with midfielder",
                "url": "https://www.bbc.com/sport/football/example",
                "domain": "bbc.com",
                "seendate": "20260630T080000Z",
            },
            {
                "title": "Unapproved transfer blog",
                "url": "https://rumours.example/story",
                "domain": "rumours.example",
                "seendate": "20260630T080000Z",
            },
        ]
    }

    evidence = parse_gdelt_articles(
        payload,
        request,
        cutoff=datetime(2026, 6, 30, 9, tzinfo=UTC),
    )

    assert len(evidence) == 1
    assert evidence[0].source_id == "bbc-sport"
    assert evidence[0].evidence_kind == "discovery"
    assert evidence[0].verification_status == "unverified_lead"


def test_bbc_feed_keeps_sign_verb_transfer_headline() -> None:
    request = ConsumerReportRequest(
        report_type="daily_football_digest",
        subject="今日球脉｜世界杯与夏季转会窗",
        report_date=date(2026, 7, 3),
    )
    feed = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item><title>Spurs sign Fernandes for club record fee</title>
        <link>https://www.bbc.co.uk/sport/football/articles/fernandes</link>
        <description>Tottenham complete a transfer for Fernandes.</description>
        <pubDate>Fri, 03 Jul 2026 07:00:00 GMT</pubDate></item>
    </channel></rss>"""

    evidence = parse_bbc_feed(
        feed,
        request,
        cutoff=datetime(2026, 7, 3, 8, tzinfo=UTC),
    )

    assert len(evidence) == 1
    assert "sign Fernandes" in evidence[0].title


def test_finish_selection_expands_chinese_focus_to_spurs_aliases() -> None:
    request = ConsumerReportRequest(
        report_type="daily_football_digest",
        subject="今日球脉｜世界杯与夏季转会窗",
        report_date=date(2026, 7, 3),
        focus=["热刺"],
    )
    now = datetime(2026, 7, 3, 8, tzinfo=UTC)
    spurs = Evidence(
        id="spurs-transfer",
        title="Spurs sign Fernandes for club record fee",
        url="https://www.bbc.co.uk/sport/football/articles/fernandes",
        published_at=now.replace(hour=6),
        source_name="BBC Sport Football",
        summary="Tottenham complete a transfer for Fernandes.",
        source_id="bbc-football-rss",
    )
    other = Evidence(
        id="other-transfer",
        title="Arsenal sign Lioness Stanway from Bayern",
        url="https://www.bbc.co.uk/sport/football/articles/stanway",
        published_at=now,
        source_name="BBC Sport Football",
        summary="Arsenal complete a transfer.",
        source_id="bbc-football-rss",
    )

    selected = finish_evidence_selection(request, [other, spurs], max_items=1)

    assert selected[0].id == "spurs-transfer"


def test_daily_selection_keeps_match_story_when_transfer_focus_is_strong() -> None:
    request = ConsumerReportRequest(
        report_type="daily_football_digest",
        subject="今日球脉｜世界杯与夏季转会窗",
        report_date=date(2026, 7, 3),
        focus=["热刺"],
    )
    now = datetime(2026, 7, 3, 8, tzinfo=UTC)
    transfers = [
        Evidence(
            id=f"spurs-transfer-{index}",
            title=f"Tottenham transfer update {index}",
            url=f"https://www.theguardian.com/football/spurs-{index}",
            published_at=now,
            source_name="The Guardian Football",
            summary="Tottenham transfer deal signing medical agreement.",
            source_id="guardian-open-platform",
        )
        for index in range(6)
    ]
    match = Evidence(
        id="portugal-croatia",
        title="Portugal 2-1 Croatia: World Cup last 32 match report",
        url="https://www.theguardian.com/football/portugal-croatia",
        published_at=now,
        source_name="The Guardian Football",
        summary="Portugal beat Croatia after a late VAR call and two goals.",
        source_id="guardian-open-platform",
    )

    selected = finish_evidence_selection(request, [*transfers, match], max_items=4)

    assert any(item.id == "portugal-croatia" for item in selected)


def test_bbc_feed_is_an_independent_approved_publisher() -> None:
    content = b"""<rss><channel><item>
      <title>World Cup team news and transfer update</title>
      <link>https://www.bbc.com/sport/football/articles/example</link>
      <description>Current team and transfer news.</description>
      <pubDate>Tue, 30 Jun 2026 08:00:00 GMT</pubDate>
    </item></channel></rss>"""
    request = ConsumerReportRequest(
        report_type="daily_football_digest",
        subject="World Cup transfer news",
        report_date=date(2026, 6, 30),
    )

    evidence = parse_bbc_feed(
        content,
        request,
        cutoff=datetime(2026, 6, 30, 9, tzinfo=UTC),
    )

    assert len(evidence) == 1
    assert evidence[0].source_id == "bbc-football-rss"
    assert evidence[0].source_independence_key == "bbc-sport"


def test_newsapi_items_are_metadata_leads_from_approved_domains() -> None:
    request = ConsumerReportRequest(
        report_type="transfer_daily",
        subject="Example Player transfer",
        report_date=date(2026, 7, 2),
    )
    payload = {
        "articles": [
            {
                "source": {"name": "Sky Sports"},
                "title": "Example Player transfer talks continue",
                "description": "Club talks are ongoing.",
                "url": "https://www.skysports.com/football/news/example",
                "publishedAt": "2026-07-02T07:00:00Z",
                "content": "This full article body must not be stored.",
            },
            {
                "source": {"name": "Blog"},
                "title": "Unapproved rumour",
                "description": "Ignore me",
                "url": "https://rumours.example/story",
                "publishedAt": "2026-07-02T07:00:00Z",
            },
        ]
    }

    evidence = parse_newsapi_articles(
        payload,
        request,
        cutoff=datetime(2026, 7, 2, 8, tzinfo=UTC),
    )

    assert len(evidence) == 1
    assert evidence[0].source_id == "sky-sports"
    assert evidence[0].evidence_kind == "discovery"
    assert evidence[0].verification_status == "unverified_lead"
    assert "full article body" not in evidence[0].summary


def test_expanded_publisher_registry_accepts_diario_sport_metadata() -> None:
    request = ConsumerReportRequest(
        report_type="transfer_daily",
        subject="Barcelona transfer news",
        report_date=date(2026, 7, 3),
    )
    payload = {
        "articles": [
            {
                "title": "Barcelona transfer talks continue",
                "url": "https://www.sport.es/en/news/barca/example",
                "domain": "sport.es",
                "seendate": "20260703T080000Z",
            }
        ]
    }

    evidence = parse_gdelt_articles(
        payload,
        request,
        cutoff=datetime(2026, 7, 3, 9, tzinfo=UTC),
    )

    assert len(evidence) == 1
    assert evidence[0].source_id == "diario-sport"
    assert evidence[0].verification_status == "unverified_lead"


def test_newsapi_domain_filter_prefers_publishers_over_official_sites() -> None:
    domains = _approved_newsapi_domains().split(",")

    assert "theguardian.com" in domains
    assert "skysports.com" in domains
    assert "sport.es" in domains
    assert "manutd.com" not in domains


def test_article_reader_extracts_bounded_text_from_approved_source() -> None:
    html = """
    <html><head><meta name="description" content="Short deck"></head>
    <body><nav>Navigation noise</nav><article>
      <p>Portugal paid tribute to Diogo Jota before kick-off.</p>
      <p>The report says the squad used his memory as inspiration.</p>
    </article><script>hidden()</script></body></html>
    """

    extracted = extract_article_text(html)

    assert "Portugal paid tribute" in extracted
    assert "Navigation noise" not in extracted
    assert "hidden()" not in extracted


def test_article_reader_refuses_unapproved_domains() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("unapproved domains must not be fetched")

    result = asyncio.run(
        read_article_excerpt(
            "https://rumours.example/story",
            transport=httpx.MockTransport(handler),
        )
    )

    assert result is None


def test_article_reader_fetches_only_excerpt_not_full_storage() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            html="""
            <html><body><article>
              <p>BBC article paragraph about a verified football event.</p>
              <p>More context that should be available to the summarizer.</p>
            </article></body></html>
            """,
        )

    result = asyncio.run(
        read_article_excerpt(
            "https://www.bbc.co.uk/sport/football/articles/example",
            transport=httpx.MockTransport(handler),
            max_chars=120,
        )
    )

    assert result is not None
    assert result.chars_read <= 120
    assert "verified football event" in result.text


def test_story_clusters_annotate_independent_transfer_leads() -> None:
    request_time = datetime(2026, 7, 2, 8, tzinfo=UTC)
    items = [
        {
            "id": "ev-1",
            "title": "Example Player transfer bid accepted",
            "url": "https://www.bbc.com/sport/football/example",
            "published_at": request_time,
            "source_name": "BBC",
            "summary": "A transfer bid has been accepted.",
            "source_id": "bbc-sport",
            "verification_status": "publisher_report",
            "source_independence_key": "bbc-sport",
        },
        {
            "id": "ev-2",
            "title": "Example Player transfer bid talks advance",
            "url": "https://www.skysports.com/football/example",
            "published_at": request_time,
            "source_name": "Sky Sports",
            "summary": "Talks over the transfer bid are advancing.",
            "source_id": "sky-sports",
            "evidence_kind": "discovery",
            "verification_status": "unverified_lead",
            "source_independence_key": "sky-sports",
        },
    ]
    evidence = _cluster_evidence([Evidence(**item) for item in items])
    _annotate_story_clusters(evidence)

    assert evidence[0].story_cluster_id == evidence[1].story_cluster_id
    assert "事件簇" in evidence[0].summary
    assert "2 个独立来源" in evidence[0].summary


def test_finish_selection_focuses_critical_entity_subjects() -> None:
    now = datetime(2026, 7, 3, 8, tzinfo=UTC)
    request = ConsumerReportRequest(
        report_type="daily_football_digest",
        subject="若塔 今日消息",
        report_date=date(2026, 7, 3),
    )
    items = [
        Evidence(
            id="critical-diogo-jota",
            title="Diogo Jota: 1996-2025",
            url="https://www.liverpoolfc.com/news/diogo-jota-1996-2025",
            published_at=now,
            source_name="Liverpool FC",
            summary="Diogo Jota passed away.",
            source_id="liverpool",
            verification_status="official",
        ),
        Evidence(
            id="bbc-jota",
            title="Victorious Portugal pay emotional tribute to Jota",
            url="https://www.bbc.co.uk/sport/football/articles/jota",
            published_at=now,
            source_name="BBC Sport",
            summary="One year after his death, Portugal remember Diogo Jota.",
            source_id="bbc-sport",
        ),
        Evidence(
            id="guardian-other",
            title="World Cup transfer live",
            url="https://www.theguardian.com/football/other",
            published_at=now,
            source_name="The Guardian",
            summary="A different football story.",
            source_id="guardian-football",
        ),
    ]

    selected = finish_evidence_selection(request, items, max_items=8)

    assert [item.id for item in selected] == ["critical-diogo-jota", "bbc-jota"]


def test_finish_selection_keeps_critical_guardrail_from_dominating_digest() -> None:
    now = datetime(2026, 7, 3, 8, tzinfo=UTC)
    request = ConsumerReportRequest(
        report_type="daily_football_digest",
        subject="World Cup knockout news",
        report_date=date(2026, 7, 3),
        focus=["Portugal Croatia"],
    )
    items = [
        Evidence(
            id="critical-diogo-jota",
            title="Diogo Jota: 1996-2025",
            url="https://www.liverpoolfc.com/news/diogo-jota-1996-2025",
            published_at=now,
            source_name="Liverpool FC",
            summary="Diogo Jota passed away.",
            source_id="liverpool",
            verification_status="official",
        ),
        Evidence(
            id="bbc-jota",
            title="Victorious Portugal pay emotional tribute to Jota",
            url="https://www.bbc.co.uk/sport/football/articles/jota",
            published_at=now,
            source_name="BBC Sport",
            summary="Portugal remember Diogo Jota after beating Croatia.",
            source_id="bbc-sport",
        ),
        Evidence(
            id="guardian-portugal-croatia",
            title="Ramos sends Portugal into last 16 as Croatia fall",
            url="https://www.theguardian.com/football/portugal-croatia",
            published_at=now,
            source_name="The Guardian",
            summary="Portugal beat Croatia in a World Cup knockout match.",
            source_id="guardian-football",
        ),
    ]

    selected = finish_evidence_selection(request, items, max_items=2)

    assert selected[0].id == "guardian-portugal-croatia"
    assert "critical-diogo-jota" not in [item.id for item in selected[:1]]
