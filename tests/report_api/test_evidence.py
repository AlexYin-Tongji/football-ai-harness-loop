from __future__ import annotations

from datetime import UTC, date, datetime

from services.report_api.domain import ConsumerReportRequest, Evidence
from services.report_api.evidence import (
    _annotate_story_clusters,
    _cluster_evidence,
    parse_bbc_feed,
    parse_gdelt_articles,
    parse_guardian_feed,
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
