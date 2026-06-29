from __future__ import annotations

from datetime import UTC, date, datetime

from services.report_api.domain import ConsumerReportRequest
from services.report_api.evidence import parse_guardian_feed

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
