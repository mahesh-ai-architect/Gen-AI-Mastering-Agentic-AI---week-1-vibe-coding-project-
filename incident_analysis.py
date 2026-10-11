"""Data preparation, metrics, and executive reports for incident logs."""

from dataclasses import dataclass
from html import escape
from io import BytesIO
import math

import pandas as pd

from sla_analysis import (
    DEFAULT_SLA_POLICY, SLAMetrics, calculate_sla_metrics, evaluate_slas,
    severity_order, sla_by_severity, validate_sla_policy,
)


REQUIRED_COLUMNS = (
    "incident_id", "opened_at", "service", "team", "severity", "region",
    "ack_minutes", "resolve_minutes", "status", "summary",
)
PERIODS = ("Last 24 hours", "Last week", "Last month")
CLOSED_STATUSES = {"resolved", "closed"}


@dataclass(frozen=True)
class Metrics:
    total: int
    mttr: float | None
    sev1: int
    ack_percent: float | None
    unresolved: int
    ack_known: int
    resolve_known: int


@dataclass
class ExecutiveReport:
    start: pd.Timestamp
    end: pd.Timestamp
    previous_start: pd.Timestamp
    current: pd.DataFrame
    previous: pd.DataFrame
    metrics: Metrics
    previous_metrics: Metrics
    services: pd.DataFrame
    sla_metrics: SLAMetrics
    previous_sla_metrics: SLAMetrics
    sla_severities: pd.DataFrame
    sla_breaches: pd.DataFrame
    markdown: str
    html: str


def load_incidents(content: bytes) -> tuple[pd.DataFrame, list[str]]:
    """Validate an upload without silently dropping incidents with unknown timings."""
    try:
        frame = pd.read_csv(BytesIO(content), dtype="string", encoding="utf-8-sig")
    except (pd.errors.EmptyDataError, pd.errors.ParserError, UnicodeDecodeError) as exc:
        raise ValueError("Unable to read the CSV. Use a UTF-8 CSV with a header row.") from exc
    frame.columns = frame.columns.str.strip()
    if frame.columns.duplicated().any():
        raise ValueError("CSV column names must be unique after trimming whitespace.")
    missing = sorted(set(REQUIRED_COLUMNS) - set(frame.columns))
    if missing:
        raise ValueError("Missing required columns: " + ", ".join(missing))
    frame = frame.loc[:, list(REQUIRED_COLUMNS)].copy()
    warnings = []
    # Naive timestamps are interpreted as UTC; explicit offsets are converted to UTC.
    frame["opened_at"] = pd.to_datetime(
        frame["opened_at"], format="mixed", errors="coerce", utc=True,
    )
    invalid_dates = int(frame["opened_at"].isna().sum())
    if invalid_dates:
        warnings.append(f"Excluded {invalid_dates} row(s) with an invalid or missing opened_at.")
        frame = frame.loc[frame["opened_at"].notna()].copy()
    for column in ("ack_minutes", "resolve_minutes"):
        values = pd.to_numeric(frame[column], errors="coerce").astype(float)
        valid = values.ge(0) & values.map(math.isfinite)
        count = int((~valid).sum())
        frame[column] = values.where(valid)
        if count:
            warnings.append(
                f"{count} row(s) have missing or invalid {column}; treated as unknown."
            )
    for column in REQUIRED_COLUMNS:
        if column == "opened_at" or column.endswith("_minutes"):
            continue
        values = frame[column].str.strip().fillna("")
        blanks = values.eq("")
        if blanks.any():
            warnings.append(f"{int(blanks.sum())} row(s) have an empty {column}.")
        frame[column] = values.mask(blanks, "Unknown" if column != "summary" else "")
    frame["severity"] = frame["severity"].str.upper()
    frame["status"] = frame["status"].str.lower()
    duplicates = frame["incident_id"].duplicated(keep=False) & frame["incident_id"].ne("Unknown")
    if duplicates.any():
        warnings.append(
            f"{int(duplicates.sum())} rows share incident IDs. All rows are retained; counts are row-based."
        )
    return frame.sort_values("opened_at", kind="stable").reset_index(drop=True), warnings


def calculate_metrics(frame: pd.DataFrame) -> Metrics:
    total = len(frame)
    mean = frame["resolve_minutes"].mean()
    return Metrics(
        total=total,
        mttr=None if pd.isna(mean) else float(mean),
        sev1=int(frame["severity"].eq("SEV1").sum()),
        ack_percent=float(frame["ack_minutes"].le(15).sum() / total * 100) if total else None,
        unresolved=int((~frame["status"].isin(CLOSED_STATUSES)).sum()),
        ack_known=int(frame["ack_minutes"].notna().sum()),
        resolve_known=int(frame["resolve_minutes"].notna().sum()),
    )


def filter_incidents(frame, *, severities, services, teams, date_range=None):
    mask = (
        frame["severity"].isin(severities)
        & frame["service"].isin(services)
        & frame["team"].isin(teams)
    )
    if date_range is not None:
        start, end = date_range
        dates = frame["opened_at"].dt.date
        mask &= dates.ge(start) & dates.le(end)
    return frame.loc[mask].copy()


def search_incidents(frame: pd.DataFrame, query: str) -> pd.DataFrame:
    query = query.strip()
    if not query:
        return frame.copy()
    matches = frame.astype(str).apply(
        lambda column: column.str.contains(query, case=False, regex=False)
    ).any(axis=1)
    return frame.loc[matches].copy()


def weekly_mttr(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["week", "mttr", "incidents"])
    # W-SUN labels Monday-start weeks. Keep missing weeks as gaps, not zero MTTR.
    return (
        frame.set_index("opened_at")
        .resample("W-MON", label="left", closed="left")
        .agg(mttr=("resolve_minutes", "mean"), incidents=("incident_id", "size"))
        .rename_axis("week").reset_index()
    )


def report_window(period: str, end) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    end = pd.Timestamp(end)
    end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
    if period == "Last 24 hours":
        start = end - pd.Timedelta(hours=24)
    elif period == "Last week":
        start = end - pd.Timedelta(days=7)
    elif period == "Last month":
        start = end - pd.DateOffset(months=1)
    else:
        raise ValueError(f"Unknown report period: {period}")
    return start, end, start - (end - start)


def format_minutes(value) -> str:
    return "N/A" if value is None or pd.isna(value) else f"{value:,.1f} min"


def format_percent(value) -> str:
    return "N/A" if value is None or pd.isna(value) else f"{value:.1f}%"


def format_sla_minutes(value) -> str:
    return "N/A" if value is None or pd.isna(value) else f"{value:,.12g} min"


def volume_change(current: int, previous: int) -> str:
    if previous == 0:
        return "No incidents in either period" if current == 0 else f"{current} incidents; prior period had none"
    change = (current - previous) / previous * 100
    return f"{change:+.1f}% vs. prior period ({previous} incidents)"


def _md(value) -> str:
    text = escape(str(value)).replace("\n", " ").replace("\r", " ")
    for character in ("\\", "|", "*", "_", "[", "]", "#", "`", "!"):
        text = text.replace(character, "\\" + character)
    return text


def _markdown_table(frame: pd.DataFrame) -> list[str]:
    return [
        "| " + " | ".join(_md(column) for column in frame.columns) + " |",
        "| " + " | ".join("---" for _ in frame.columns) + " |",
        *["| " + " | ".join(_md(value) for value in row) + " |" for row in frame.itertuples(index=False, name=None)],
    ]


def build_executive_report(
    frame: pd.DataFrame, *, services: list[str], period: str, end, scope: str,
    sla_policy: dict | None = None,
) -> ExecutiveReport:
    """Produce a factual, reproducible report from an incident-file snapshot."""
    start, end, previous_start = report_window(period, end)
    policy = validate_sla_policy(DEFAULT_SLA_POLICY if sla_policy is None else sla_policy)
    scoped = evaluate_slas(frame.loc[frame["service"].isin(services)], policy)
    current = scoped.loc[scoped["opened_at"].gt(start) & scoped["opened_at"].le(end)].copy()
    previous = scoped.loc[
        scoped["opened_at"].gt(previous_start) & scoped["opened_at"].le(start)
    ].copy()
    metrics, prior = calculate_metrics(current), calculate_metrics(previous)
    sla, previous_sla = calculate_sla_metrics(current), calculate_sla_metrics(previous)
    severity_slas = sla_by_severity(current, policy)
    breaches = current.loc[current["sla_status"].eq("Breached")].sort_values("opened_at", ascending=False)
    rows = []
    for service in sorted(set(services)):
        service_frame = current.loc[current["service"].eq(service)]
        previous_frame = previous.loc[previous["service"].eq(service)]
        service_metrics = calculate_metrics(service_frame)
        service_sla = calculate_sla_metrics(service_frame)
        previous_service_sla = calculate_sla_metrics(previous_frame)
        previous_count = len(previous_frame)
        rows.append({
            "service": service, "incidents": service_metrics.total,
            "previous_incidents": previous_count,
            "change": service_metrics.total - previous_count,
            "mttr": service_metrics.mttr, "sev1": service_metrics.sev1,
            "ack_percent": service_metrics.ack_percent,
            "unresolved": service_metrics.unresolved,
            "sla_breached": service_sla.breached,
            "previous_sla_breached": previous_service_sla.breached,
            "ack_sla_breached": service_sla.ack_breached,
            "resolve_sla_breached": service_sla.resolve_breached,
            "sla_met": service_sla.met, "sla_unknown": service_sla.unknown,
            "sla_not_configured": service_sla.not_configured,
        })
    service_table = pd.DataFrame(rows, columns=[
        "service", "incidents", "previous_incidents", "change", "mttr",
        "sev1", "ack_percent", "unresolved",
        "sla_breached", "previous_sla_breached", "ack_sla_breached", "resolve_sla_breached",
        "sla_met", "sla_unknown", "sla_not_configured",
    ]).sort_values(["incidents", "service"], ascending=[False, True], kind="stable")
    highlights, actions = [], []
    if metrics.total:
        service_count = current["service"].nunique()
        service_label = "service" if service_count == 1 else "services"
        incident_label = "incident" if metrics.total == 1 else "incidents"
        highlights.append(
            f"{metrics.total} {incident_label} across {service_count} {service_label}; "
            f"{volume_change(metrics.total, prior.total)}."
        )
        highlights.append(
            f"MTTR was {format_minutes(metrics.mttr)}; "
            f"{format_percent(metrics.ack_percent)} of incidents were acknowledged within 15 minutes."
        )
        highlights.append(
            f"{metrics.sev1} SEV1 incidents; {metrics.unresolved} incidents in this opened-period "
            "cohort are not marked resolved or closed in the uploaded snapshot."
        )
        top = service_table.iloc[0]
        highlights.append(
            f"{top['service']} had the most incidents: {int(top['incidents'])} "
            f"({top['incidents'] / metrics.total:.1%} of the selected scope)."
        )
        if metrics.mttr is not None and prior.mttr is not None:
            highlights.append(
                f"MTTR changed by {metrics.mttr - prior.mttr:+.1f} minutes "
                f"from {format_minutes(prior.mttr)} in the prior period."
            )
        if metrics.sev1:
            actions.append("Review SEV1 follow-ups with service owners and track corrective actions to completion.")
        if metrics.unresolved:
            actions.append("Confirm owners and next updates for incidents not marked resolved or closed.")
        late = int(current["ack_minutes"].gt(15).sum())
        if late:
            actions.append(
                f"Review paging and escalation for the {late} incidents acknowledged after 15 minutes."
            )
        actions.append(f"Review recurring incident themes for {top['service']} with its owning teams.")
    else:
        highlights.append(
            "No incidents were opened in this period for the selected scope. "
            "This describes the loaded file, and does not establish service availability or absence of older open incidents."
        )
        highlights.append(volume_change(metrics.total, prior.total) + ".")
        actions.append("Confirm the log covers the reporting period before drawing an operational conclusion.")
    if metrics.ack_known < metrics.total or metrics.resolve_known < metrics.total:
        actions.append("Complete missing or invalid timing fields to improve confidence in response metrics.")
    breach_label = "incident" if sla.breached == 1 else "incidents"
    highlights.append(
        f"SLA breaches: {sla.breached} {breach_label} ({sla.ack_breached} acknowledgement; "
        f"{sla.resolve_breached} resolution). An incident breaching both is counted once in the total. "
        f"The prior period had {previous_sla.breached} breached incidents under the same policy "
        f"({sla.breached - previous_sla.breached:+d} change)."
    )
    highlights.append(
        f"SLA outcomes: {sla.met} met both targets; {sla.unknown} unknown; "
        f"{sla.not_configured} without a configured SLA."
    )
    if sla.breached:
        noisiest = service_table.sort_values(["sla_breached", "service"], ascending=[False, True]).iloc[0]
        actions.append(
            f"Prioritize an SLA breach review for {noisiest['service']} "
            f"({int(noisiest['sla_breached'])} breached incidents); review acknowledgement and resolution delays with its owners."
        )
    if sla.unknown:
        actions.append("Complete unknown timing fields before concluding that the affected incidents met both SLA targets.")
    if sla.not_configured:
        actions.append("Define SLA targets for severities marked Not configured.")

    themes = current.loc[current["summary"].ne("")].groupby(["service", "summary"]).size()
    themes = themes.loc[themes.gt(1)].sort_values(ascending=False).head(5)
    theme_lines = [f"{service}: {summary} ({count} incidents)" for (service, summary), count in themes.items()]
    notable = current.loc[
        current["severity"].eq("SEV1") | ~current["status"].isin(CLOSED_STATUSES)
    ].sort_values("opened_at", ascending=False).head(10)
    notable_lines = [
        f"{row.incident_id} | {row.opened_at:%Y-%m-%d %H:%M UTC} | {row.service} | "
        f"{row.severity} | {row.status} | {row.summary}"
        for row in notable.itertuples()
    ]
    notes = (
        "Periods include incidents opened after the start and up to and including the end. "
        "Last week means a rolling 7 days; last month means one calendar month back. "
        "The comparison window has the same duration and ends at the current window's start. "
        "MTTR is the mean of valid, nonnegative resolve_minutes values, regardless of status; "
        "it is not total outage duration. Acknowledgement compliance uses all incidents as its denominator; "
        "unknown acknowledgement times do not meet the threshold. Status is the current file snapshot, "
        "not reconstructed historical state. This report does not infer customer impact or root causes. "
        "All timestamps are UTC; timestamps without an offset are interpreted as UTC."
        " SLA results use the active severity policy for both periods, not a historical policy audit. "
        "A duration breaches only when it is greater than its target; equality meets the SLA. "
        "Unknown durations do not establish compliance or a breach, but do not hide a confirmed breach of the other target. "
        "Missing policies are Not configured. Resolution SLAs compare recorded resolve_minutes regardless of status; "
        "this is snapshot analysis, not a live countdown for unresolved incidents. "
        "The fixed 15-minute acknowledgement KPI is separate from severity-based SLA targets."
    )
    coverage = (
        f"Timing coverage: {metrics.resolve_known}/{metrics.total} valid resolution durations; "
        f"{metrics.ack_known}/{metrics.total} valid acknowledgement durations."
    )
    dates = f"{start:%Y-%m-%d %H:%M UTC} to {end:%Y-%m-%d %H:%M UTC}"
    prior_dates = f"{previous_start:%Y-%m-%d %H:%M UTC} to {start:%Y-%m-%d %H:%M UTC}"
    md = [
        "# Incident Executive Report", "", f"**Period:** {period} — {dates}", "",
        f"**Comparison:** {prior_dates}", "", f"**Scope:** {_md(scope)}", "",
        f"**Services:** {', '.join(_md(s) for s in services) or 'None'}",
        "", "## Executive summary", "", *[f"- {_md(line)}" for line in highlights],
        "", "## Service performance", "",
        "| Service | Incidents | Prior | Change | MTTR | SEV1 | Ack ≤ 15 min | Unresolved | SLA breaches |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in service_table.itertuples():
        md.append(
            f"| {_md(row.service)} | {row.incidents} | {row.previous_incidents} | {row.change:+d} "
            f"| {format_minutes(row.mttr)} | {row.sev1} | {format_percent(row.ack_percent)} | {row.unresolved} | {row.sla_breached} |"
        )
    sla_display = severity_slas.rename(columns={
        "severity": "Severity", "ack_target": "Ack target", "resolve_target": "Resolve target",
        "incidents": "Incidents", "breached": "SLA breaches", "ack_breached": "Ack breaches",
        "resolve_breached": "Resolve breaches", "met": "Met", "unknown": "Unknown",
        "not_configured": "No SLA",
    }).copy()
    for column in ("Ack target", "Resolve target"):
        sla_display[column] = sla_display[column].map(lambda value: "Not configured" if pd.isna(value) else format_sla_minutes(value))
    breach_display = pd.DataFrame([
        {
            "Incident": row.incident_id, "Service": row.service, "Severity": row.severity,
            "Ack actual / target": f"{format_sla_minutes(row.ack_minutes)} / {format_sla_minutes(row.ack_sla_minutes)}",
            "Ack overdue": format_sla_minutes(row.ack_overdue_minutes),
            "Resolve actual / target": f"{format_sla_minutes(row.resolve_minutes)} / {format_sla_minutes(row.resolve_sla_minutes)}",
            "Resolve overdue": format_sla_minutes(row.resolve_overdue_minutes),
        }
        for row in breaches.head(20).itertuples()
    ])
    md.extend(["", "## SLA performance by severity", "", *_markdown_table(sla_display)])
    md.extend(["", "## SLA breached incidents", ""])
    if breaches.empty:
        md.append("No confirmed SLA breaches in this period and scope.")
    else:
        md.extend([
            f"Showing the {len(breach_display)} most recent of {len(breaches)} breached incidents. The SLA breach CSV includes all breaches.",
            "", *_markdown_table(breach_display),
        ])
    sections = [
        ("Recurring themes", theme_lines or ["No repeated summaries in the selected period."]),
        ("Notable incidents", notable_lines or ["No SEV1 or unresolved incidents in the selected period."]),
        ("Recommended actions", actions),
    ]
    for title, lines in sections:
        md.extend(["", f"## {title}", "", *[f"- {_md(line)}" for line in lines]])
    md.extend(["", "## Methodology and coverage", "", coverage, "", notes])
    markdown = "\n".join(md) + "\n"

    def html_list(lines):
        return "<ul>" + "".join(f"<li>{escape(line)}</li>" for line in lines) + "</ul>"

    display = service_table.loc[:, [
        "service", "incidents", "previous_incidents", "change", "mttr", "sev1",
        "ack_percent", "unresolved", "sla_breached",
    ]].copy().rename(columns={
        "service": "Service", "incidents": "Incidents", "previous_incidents": "Prior",
        "change": "Change", "mttr": "MTTR", "sev1": "SEV1",
        "ack_percent": "Ack ≤ 15 min", "unresolved": "Unresolved",
        "sla_breached": "SLA breaches",
    })
    display["MTTR"] = display["MTTR"].map(format_minutes)
    display["Ack ≤ 15 min"] = display["Ack ≤ 15 min"].map(format_percent)
    html_sections = "".join(f"<h2>{escape(title)}</h2>{html_list(lines)}" for title, lines in sections)
    breach_html = (
        "<p>No confirmed SLA breaches in this period and scope.</p>" if breaches.empty else
        f"<p>Showing the {len(breach_display)} most recent of {len(breaches)} breached incidents. "
        "The SLA breach CSV includes all breaches.</p><div class=\"breaches\">" +
        breach_display.to_html(index=False, escape=True, border=0) + "</div>"
    )
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Incident Executive Report</title><style>
body{{font:15px/1.6 system-ui,sans-serif;color:#172b4d;margin:48px auto;padding:0 28px;max-width:1050px}}
h1{{font-size:32px;color:#123b73}}h2{{font-size:20px;margin-top:30px}}p{{margin:8px 0}}
table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:10px;text-align:left;border-bottom:1px solid #dce3ed}}
th{{background:#eef3f9}}li{{margin:6px 0}}.notes{{color:#53657c;font-size:13px}}
.breaches td{{background:#fef2f2;color:#991b1b}}
@media print{{body{{margin:0;max-width:none;padding:0}}h2{{break-after:avoid}}tr{{break-inside:avoid}}}}
</style></head><body><h1>Incident Executive Report</h1>
<p><strong>Period:</strong> {escape(period)} — {dates}</p><p><strong>Comparison:</strong> {prior_dates}</p>
<p><strong>Scope:</strong> {escape(scope)}</p><p><strong>Services:</strong> {escape(', '.join(services) or 'None')}</p>
<h2>Executive summary</h2>{html_list(highlights)}<h2>Service performance</h2>
{display.to_html(index=False, escape=True, border=0)}
<h2>SLA performance by severity</h2>{sla_display.to_html(index=False, escape=True, border=0)}
<h2>SLA breached incidents</h2>{breach_html}{html_sections}
<h2>Methodology and coverage</h2><p class="notes">{escape(coverage)}</p><p class="notes">{escape(notes)}</p>
</body></html>"""
    return ExecutiveReport(
        start=start, end=end, previous_start=previous_start, current=current, previous=previous,
        metrics=metrics, previous_metrics=prior, services=service_table,
        sla_metrics=sla, previous_sla_metrics=previous_sla, sla_severities=severity_slas,
        sla_breaches=breaches, markdown=markdown, html=html,
    )
