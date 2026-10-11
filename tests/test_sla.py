from io import BytesIO
import json

import pandas as pd
import pytest

from incident_analysis import build_executive_report, load_incidents
from sla_analysis import (
    DEFAULT_SLA_POLICY, calculate_sla_metrics, evaluate_slas,
    sla_by_severity, validate_sla_policy,
)


def incidents(rows):
    records = []
    for index, values in enumerate(rows):
        records.append({
            "incident_id": f"INC-{index}", "opened_at": "2026-10-06T12:00:00Z",
            "service": "api", "team": "ops", "severity": "SEV1", "region": "us-east",
            "ack_minutes": 15, "resolve_minutes": 60, "status": "resolved", "summary": "Example",
            **values,
        })
    content = pd.DataFrame(records).to_csv(index=False).encode()
    return load_incidents(content)[0]


def test_equal_targets_meet_sla_and_fractional_excess_breaches():
    frame = incidents([
        {}, {"ack_minutes": 15.01}, {"resolve_minutes": 60.01},
        {"ack_minutes": 16, "resolve_minutes": 65},
    ])
    evaluated = evaluate_slas(frame, DEFAULT_SLA_POLICY)
    assert evaluated["sla_status"].tolist() == ["Met", "Breached", "Breached", "Breached"]
    assert evaluated["ack_overdue_minutes"].tolist() == pytest.approx([0, .01, 0, 1])
    assert evaluated["resolve_overdue_minutes"].tolist() == pytest.approx([0, 0, .01, 5])
    metrics = calculate_sla_metrics(evaluated)
    assert metrics.breached == 3
    assert metrics.ack_breached == metrics.resolve_breached == 2
    assert metrics.met == 1


def test_missing_timings_remain_unknown_without_hiding_confirmed_breach():
    frame = incidents([
        {"ack_minutes": None}, {"resolve_minutes": None},
        {"ack_minutes": 16, "resolve_minutes": None},
        {"ack_minutes": None, "resolve_minutes": 61},
        {"ack_minutes": -1, "resolve_minutes": "invalid"},
    ])
    evaluated = evaluate_slas(frame, DEFAULT_SLA_POLICY)
    assert evaluated["sla_status"].tolist() == ["Unknown", "Unknown", "Breached", "Breached", "Unknown"]
    metrics = calculate_sla_metrics(evaluated)
    assert metrics.breached == 2 and metrics.unknown == 3 and metrics.met == 0
    assert pd.isna(evaluated.iloc[0]["ack_overdue_minutes"])


def test_disabled_and_unrecognized_severities_are_not_configured():
    frame = incidents([{}, {"severity": "SEV9", "ack_minutes": 9999}])
    evaluated = evaluate_slas(frame, {"SEV1": {"ack_minutes": 15, "resolve_minutes": 60}})
    assert evaluated["sla_status"].tolist() == ["Met", "Not configured"]
    assert pd.isna(evaluated.iloc[1]["ack_sla_minutes"])
    assert calculate_sla_metrics(evaluate_slas(frame, {})).not_configured == 2


def test_sla_targets_follow_severity_and_recalculation_replaces_old_results():
    frame = incidents([{}, {"severity": "SEV2"}])
    policy = {"SEV1": {"ack_minutes": 10, "resolve_minutes": 50}, "SEV2": {"ack_minutes": 20, "resolve_minutes": 70}}
    evaluated = evaluate_slas(frame, policy)
    assert evaluated["sla_status"].tolist() == ["Breached", "Met"]
    recalculated = evaluate_slas(evaluated, {"SEV1": {"ack_minutes": 20, "resolve_minutes": 70}})
    assert recalculated["sla_status"].tolist() == ["Met", "Not configured"]
    assert "sla_status" not in frame.columns


def test_zero_targets_and_non_contiguous_indices():
    frame = incidents([{}, {"ack_minutes": 0, "resolve_minutes": 0}, {"ack_minutes": 1, "resolve_minutes": 1}]).iloc[1:]
    evaluated = evaluate_slas(frame, {"SEV1": {"ack_minutes": 0, "resolve_minutes": 0}})
    assert evaluated.index.tolist() == [1, 2]
    assert evaluated["sla_status"].tolist() == ["Met", "Breached"]


def test_sla_summary_totals_and_empty_scope():
    frame = incidents([{}, {"ack_minutes": 16}, {"resolve_minutes": None}, {"severity": "SEV9"}])
    evaluated = evaluate_slas(frame, DEFAULT_SLA_POLICY)
    metrics = calculate_sla_metrics(evaluated)
    assert metrics.breached + metrics.met + metrics.unknown + metrics.not_configured == len(frame)
    table = sla_by_severity(evaluated, DEFAULT_SLA_POLICY)
    assert table["incidents"].sum() == 4
    assert table["breached"].sum() == metrics.breached
    assert table.set_index("severity").loc["SEV9", "not_configured"] == 1
    empty = evaluate_slas(frame.iloc[:0], {})
    assert calculate_sla_metrics(empty).breached == 0
    assert sla_by_severity(empty, {}).empty


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan"), True, "15", None, 10**400])
def test_invalid_sla_targets_are_rejected(value):
    with pytest.raises(ValueError, match="finite, nonnegative"):
        validate_sla_policy({"SEV1": {"ack_minutes": value, "resolve_minutes": 60}})


@pytest.mark.parametrize("policy", [[], {"": {}}, {"SEV1": {"ack_minutes": 15}}, {"SEV1": {}, " sev1 ": {}}])
def test_malformed_sla_policies_are_rejected(policy):
    with pytest.raises(ValueError):
        validate_sla_policy(policy)


def test_policy_json_roundtrip_and_normalization():
    encoded = json.dumps({" sev1 ": {"ack_minutes": 15, "resolve_minutes": 60}})
    assert validate_sla_policy(json.load(BytesIO(encoded.encode()))) == {"SEV1": {"ack_minutes": 15.0, "resolve_minutes": 60.0}}


def test_executive_report_applies_same_policy_to_both_periods_and_scopes_services():
    frame = incidents([
        {"ack_minutes": 16, "resolve_minutes": 70},
        {"opened_at": "2026-10-05T12:00:00Z", "ack_minutes": 16},
        {"service": "web", "ack_minutes": 99},
        {"resolve_minutes": None},
    ])
    policy = {"SEV1": {"ack_minutes": 15, "resolve_minutes": 60}}
    report = build_executive_report(frame, services=["api"], period="Last 24 hours", end="2026-10-06T12:00Z", scope="Test", sla_policy=policy)
    assert report.sla_metrics.breached == report.previous_sla_metrics.breached == 1
    assert report.sla_metrics.unknown == 1
    assert report.sla_breaches["incident_id"].tolist() == ["INC-0"]
    assert report.services["sla_breached"].sum() == 1
    assert report.services["ack_sla_breached"].sum() == 1
    assert report.services["resolve_sla_breached"].sum() == 1
    assert report.services["previous_sla_breached"].sum() == 1
    for output in (report.markdown, report.html):
        assert "SLA performance by severity" in output
        assert "SLA breached incidents" in output
        assert "SLA breaches: 1 incident" in output
        assert "15 min" in output and "60 min" in output
        assert "Prioritize an SLA breach review for api" in output
    assert "INC-2" not in report.markdown
    relaxed = {"SEV1": {"ack_minutes": 20, "resolve_minutes": 100}}
    report = build_executive_report(report.current, services=["api"], period="Last 24 hours", end="2026-10-06T12:00Z", scope="Test", sla_policy=relaxed)
    assert report.sla_metrics.breached == 0


def test_empty_policy_stays_disabled_in_executive_report():
    frame = incidents([{"ack_minutes": 99999, "resolve_minutes": 99999}])
    report = build_executive_report(frame, services=["api"], period="Last week", end=frame.opened_at.max(), scope="Test", sla_policy={})
    assert report.sla_metrics.breached == 0
    assert report.sla_metrics.not_configured == 1
    assert report.sla_breaches.empty
    assert "Not configured" in report.markdown


def test_breach_export_has_all_incidents_even_when_report_preview_is_limited():
    frame = incidents([{"ack_minutes": 16} for _ in range(25)])
    report = build_executive_report(frame, services=["api"], period="Last week", end=frame.opened_at.max(), scope="Test")
    assert len(report.sla_breaches) == 25
    assert "20 most recent of 25" in report.markdown
    assert "ack_sla_minutes" in report.sla_breaches.columns
    assert "ack_overdue_minutes" in report.sla_breaches.columns


def test_report_preserves_small_fractional_breaches():
    frame = incidents([{"ack_minutes": 15.01}])
    report = build_executive_report(frame, services=["api"], period="Last week", end=frame.opened_at.max(), scope="Test")
    for output in (report.markdown, report.html):
        assert "15.01 min / 15 min" in output
        assert "0.01 min" in output
