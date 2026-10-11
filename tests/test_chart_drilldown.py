import pandas as pd
import pytest

from chart_drilldown import acknowledgement_bins, filter_chart_incidents


@pytest.fixture
def frame():
    return pd.DataFrame({
        "incident_id": ["A", "B", "C", "D", "E"],
        "severity": ["SEV1", "SEV1", "SEV2", "SEV2", "SEV1"],
        "service": ["api", "web", "api", "web", "api"],
        "sla_status": ["Breached", "Met", "Met", "Breached", "Unknown"],
        "opened_at": pd.to_datetime([
            "2026-10-05T00:00Z", "2026-10-11T23:59:59Z", "2026-10-12T00:00Z",
            "2026-10-12T12:00Z", "2026-10-06T12:00Z",
        ], utc=True, format="mixed"),
        "ack_minutes": [0.0, 15.0, 30.0, 45.0, float("nan")],
    }, index=[2, 5, 8, 11, 14])


def point(*values):
    return {"customdata": list(values), "point_number": 0, "curve_number": 0}


def test_severity_and_service_selections_combine(frame):
    selected, labels = filter_chart_incidents(frame, {"severity": [point("SEV1")], "service": [point("api")]})
    assert selected["incident_id"].tolist() == ["A", "E"]
    assert labels == ["Severity: SEV1", "Service: api"]


def test_multiple_points_within_chart_are_unioned(frame):
    selected, _ = filter_chart_incidents(frame, {"service": [point("api"), point("web")]})
    assert len(selected) == 5
    selected, _ = filter_chart_incidents(frame, {"severity": [point("SEV1"), point("SEV2")], "service": [point("web")]})
    assert selected["incident_id"].tolist() == ["B", "D"]


def test_sla_segments_match_exact_pairs_without_cross_products(frame):
    selected, _ = filter_chart_incidents(frame, {"sla": [point("SEV1", "Breached"), point("SEV2", "Met")]})
    assert selected["incident_id"].tolist() == ["A", "C"]


def test_week_selection_includes_monday_and_sunday_but_not_next_monday(frame):
    selected, labels = filter_chart_incidents(frame, {"week": [point("2026-10-05T00:00:00Z")]})
    assert selected["incident_id"].tolist() == ["A", "B", "E"]
    assert labels == ["Week starting 2026-10-05 UTC"]


def test_histogram_boundary_points_belong_to_exactly_one_bin(frame):
    bins = acknowledgement_bins(frame, bins=3)
    assert bins["incidents"].tolist() == [1, 1, 2]
    records = []
    for row in bins.itertuples():
        selected, _ = filter_chart_incidents(frame, {"ack": [point(row.bin_start, row.bin_end, row.include_right)]})
        assert len(selected) == row.incidents
        records.extend(selected["incident_id"])
    assert sorted(records) == ["A", "B", "C", "D"]
    assert len(records) == len(set(records))


def test_constant_acknowledgement_values_and_missing_only(frame):
    frame["ack_minutes"] = 15.0
    bins = acknowledgement_bins(frame)
    row = bins.loc[bins["incidents"].gt(0)].iloc[0]
    selected, _ = filter_chart_incidents(frame, {"ack": [point(row.bin_start, row.bin_end, row.include_right)]})
    assert len(selected) == 5
    frame["ack_minutes"] = float("nan")
    assert acknowledgement_bins(frame).empty


def test_numeric_metadata_serialized_from_browser_typed_array(frame):
    selected, _ = filter_chart_incidents(frame, {"ack": [{"customdata": {"0": 0.0, "1": 15.0, "2": 0.0}}]})
    assert selected["incident_id"].tolist() == ["A"]


def test_missing_metadata_does_not_show_unrelated_records(frame):
    for selections in ({"severity": [{}]}, {"ack": [point(30, 0, 0)]}, {"week": [point("invalid")]}, {"invalid": [point("api")]}):
        selected, labels = filter_chart_incidents(frame, selections)
        assert selected.empty
        assert "Selection unavailable" in labels[0]


def test_no_selection_returns_sidebar_scope_and_no_matches_are_safe(frame):
    selected, labels = filter_chart_incidents(frame, {"severity": []})
    assert selected.equals(frame) and not labels
    selected, _ = filter_chart_incidents(frame, {"service": [point("missing-service")]})
    assert selected.empty
    selected, _ = filter_chart_incidents(frame.iloc[:0], {"severity": [point("SEV1")]})
    assert selected.empty
