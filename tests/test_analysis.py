from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from incident_analysis import (
    build_executive_report, calculate_metrics, filter_incidents, load_incidents,
    report_window, search_incidents, weekly_mttr,
)


SAMPLE = Path(__file__).resolve().parents[1] / "incident_log_sample.csv"
HEADER = "incident_id,opened_at,service,team,severity,region,ack_minutes,resolve_minutes,status,summary\n"


def data(*rows):
    return load_incidents((HEADER + "\n".join(rows)).encode())[0]


@pytest.fixture
def sample():
    return load_incidents(SAMPLE.read_bytes())[0]


def test_sample_metrics(sample):
    metrics = calculate_metrics(sample)
    assert metrics.total == 180
    assert metrics.mttr == pytest.approx(1123.2166666667)
    assert metrics.sev1 == 8
    assert metrics.ack_percent == 10
    assert metrics.unresolved == 6


@pytest.mark.parametrize("content", [b"", b"incident_id,service\nINC-1,api", b"\xff\xfe\xff"])
def test_invalid_files_are_actionable(content):
    with pytest.raises(ValueError, match="CSV|required columns"):
        load_incidents(content)


def test_bad_dates_and_timings_keep_denominator_honest():
    frame, notes = load_incidents((HEADER +
        "INC-1,2026-10-01 01:00,api,ops,sev1,us,15,60,resolved,First\n"
        "INC-2,2026-10-01 02:00,api,ops,SEV2,us,-1,invalid,open,Second\n"
        "INC-3,2026-10-01 03:00,api,ops,SEV2,us,inf,-10,mitigated,Third\n"
        "INC-4,not-a-date,api,ops,SEV2,us,1,3,resolved,Fourth\n"
    ).encode())
    metrics = calculate_metrics(frame)
    assert metrics.total == 3
    assert metrics.sev1 == 1
    assert metrics.mttr == 60
    assert metrics.ack_percent == pytest.approx(100 / 3)
    assert metrics.ack_known == metrics.resolve_known == 1
    assert metrics.unresolved == 2
    assert len(notes) == 3


def test_blank_metadata_duplicate_ids_and_offsets():
    frame, notes = load_incidents((HEADER +
        "INC-1,2026-10-01T01:00:00-05:00, api , ops , sev1 ,us,0,0, Resolved ,hello\n"
        "INC-1,2026-10-01T06:00:00Z,,,SEV4,,0,0,,\n"
    ).encode())
    assert frame.loc[0, "opened_at"] == pd.Timestamp("2026-10-01T06:00:00Z")
    assert frame.loc[0, "severity"] == "SEV1"
    assert frame.loc[0, "status"] == "resolved"
    assert frame.loc[1, "service"] == "Unknown"
    assert any("share incident IDs" in note for note in notes)
    assert len(frame) == 2


def test_empty_file_with_headers_has_empty_metrics():
    frame, notes = load_incidents(HEADER.encode())
    metrics = calculate_metrics(frame)
    assert frame.empty and not notes
    assert metrics.total == 0
    assert metrics.mttr is None and metrics.ack_percent is None


def test_filters_include_entire_end_day_and_empty_selections():
    frame = data(
        "I-1,2026-10-01 23:59:59,api,ops,SEV1,us,15,10,resolved,Last minute",
        "I-2,2026-10-02 00:00:00,api,ops,SEV1,us,15,10,resolved,Next day",
        "I-3,2026-10-01 12:00:00,web,ops,SEV1,us,15,10,resolved,Other service",
    )
    arguments = dict(severities=["SEV1"], services=["api"], teams=["ops"], date_range=(date(2026, 10, 1), date(2026, 10, 1)))
    assert filter_incidents(frame, **arguments)["incident_id"].tolist() == ["I-1"]
    arguments["services"] = []
    assert filter_incidents(frame, **arguments).empty


def test_search_is_case_insensitive_literal_and_searches_all_columns(sample):
    assert search_incidents(sample, "inc-1144")["incident_id"].tolist() == ["INC-1144"]
    assert search_incidents(sample, "SMS")["summary"].str.contains("SMS").all()
    assert search_incidents(sample, "[.*").empty
    assert len(search_incidents(sample, "  ")) == 180


def test_weekly_mttr_uses_monday_weeks_and_preserves_gaps():
    frame = data(
        "I-1,2026-09-28 00:00,api,ops,SEV1,us,1,10,resolved,Monday",
        "I-2,2026-10-04 23:59,api,ops,SEV1,us,1,30,resolved,Sunday",
        "I-3,2026-10-12 00:00,api,ops,SEV1,us,1,90,resolved,Later Monday",
    )
    trend = weekly_mttr(frame)
    assert trend["week"].dt.day.tolist() == [28, 5, 12]
    assert trend.loc[0, "mttr"] == 20
    assert pd.isna(trend.loc[1, "mttr"])
    assert trend["incidents"].tolist() == [2, 0, 1]


def test_report_windows_have_no_overlap_and_include_latest_incident():
    frame = data(
        "I-1,2026-10-04 12:00,api,ops,SEV1,us,15,10,resolved,Outside comparison",
        "I-2,2026-10-05 12:00,api,ops,SEV1,us,15,20,resolved,At start",
        "I-3,2026-10-05 12:01,api,ops,SEV1,us,15,30,open,Inside",
        "I-4,2026-10-06 12:00,api,ops,SEV1,us,15,40,resolved,At end",
        "I-5,2026-10-06 12:01,api,ops,SEV1,us,15,50,resolved,After end",
    )
    report = build_executive_report(frame, services=["api", "quiet-service"], period="Last 24 hours", end="2026-10-06 12:00Z", scope="Test")
    assert report.current["incident_id"].tolist() == ["I-3", "I-4"]
    assert report.previous["incident_id"].tolist() == ["I-2"]
    assert report.metrics.mttr == 35
    assert report.metrics.unresolved == 1
    assert report.services.set_index("service").loc["quiet-service", "incidents"] == 0
    assert report.services["incidents"].sum() == report.metrics.total
    assert "+100.0%" in report.markdown
    assert "Recommended actions" in report.html


@pytest.mark.parametrize("period,expected", [("Last 24 hours", 3), ("Last week", 16), ("Last month", 65)])
def test_sample_reports(period, expected, sample):
    report = build_executive_report(sample, services=sorted(sample.service.unique()), period=period, end=sample.opened_at.max(), scope="Sample")
    assert report.metrics.total == expected
    assert report.services["incidents"].sum() == expected


def test_month_end_and_timezone_conversion():
    start, end, prior = report_window("Last month", "2024-03-31T12:00:00-05:00")
    assert start == pd.Timestamp("2024-02-29T17:00:00Z")
    assert end == pd.Timestamp("2024-03-31T17:00:00Z")
    assert start - prior == end - start
    with pytest.raises(ValueError, match="Unknown report period"):
        report_window("Yesterday", end)


def test_report_is_service_scoped_and_exports_escape_uploaded_html():
    frame = data(
        'I-1,2026-10-06 12:00,api,ops,SEV1,us,20,40,open,<script>alert(1)</script>',
        'I-2,2026-10-06 12:00,web,ops,SEV1,us,20,40,open,Unselected service',
    )
    report = build_executive_report(frame, services=["api"], period="Last week", end=frame.opened_at.max(), scope="Test")
    assert report.metrics.total == 1
    assert "Unselected service" not in report.markdown
    assert "<script>" not in report.html
    assert "&lt;script&gt;" in report.html


def test_empty_report_does_not_claim_uptime(sample):
    report = build_executive_report(sample, services=["checkout"], period="Last 24 hours", end="2027-01-01T00:00Z", scope="Test")
    assert report.metrics.total == 0
    assert "does not establish service availability" in report.markdown
    assert "N/A" in report.html
