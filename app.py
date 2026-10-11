"""Run with: streamlit run app.py"""

from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from chart_drilldown import acknowledgement_bins, filter_chart_incidents
from incident_analysis import (
    PERIODS, build_executive_report, calculate_metrics, filter_incidents,
    format_minutes, format_percent, load_incidents, search_incidents,
    severity_order, weekly_mttr,
)
from sla_analysis import (
    DEFAULT_SLA_POLICY, SLA_COLORS, SLA_STATUSES, calculate_sla_metrics,
    evaluate_slas, highlight_sla_row, validate_sla_policy,
)


BASE_DIR = Path(__file__).resolve().parent
SEVERITY_COLORS = {"SEV1": "#DC3545", "SEV2": "#D97706", "SEV3": "#2563EB", "SEV4": "#8294AE"}
PLOT_CONFIG = {"displaylogo": False, "scrollZoom": False}

st.set_page_config(page_title="Incident Log Analyzer", layout="wide", initial_sidebar_state="expanded")
st.markdown("""
<style>
  .block-container {padding-top: 2.5rem; padding-bottom: 3rem; max-width: 1500px;}
  h1 {letter-spacing: -0.035em; font-weight: 700;}
  h2, h3 {letter-spacing: -0.02em;}
  [data-testid="stMetric"] {background: white; padding: 1.1rem 1.3rem;
      border: 1px solid #E1E7F0; border-radius: 10px; min-height: 125px;}
  [data-testid="stMetricLabel"] {color: #52647C;}
  [data-testid="stMetricValue"] {font-weight: 650;}
  [data-testid="stTabs"] {margin-top: 1rem;}
  .eyebrow {color: #52647C; font-size: 0.75rem; font-weight: 650;
      letter-spacing: 0.14em; margin-bottom: 0.3rem;}
</style>
""", unsafe_allow_html=True)


@st.cache_data(show_spinner=False)
def read_data(content: bytes):
    return load_incidents(content)


def polish(figure, *, height=330):
    figure.update_layout(
        height=height, margin=dict(l=10, r=20, t=35, b=15),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Arial, sans-serif", color="#52647C", size=12),
        showlegend=False, hoverlabel=dict(bgcolor="white"),
    )
    figure.update_xaxes(showgrid=False, zeroline=False)
    figure.update_yaxes(gridcolor="#E8EDF4", zeroline=False)
    return figure


def csv_bytes(frame):
    return frame.to_csv(index=False).encode("utf-8-sig")


def selectable_chart(figure, kind, context):
    epoch = st.session_state.get("chart_selection_epoch", 0)
    figure.update_layout(clickmode="event+select")
    event = st.plotly_chart(
        figure, width="stretch", config=PLOT_CONFIG,
        key=f"chart_{kind}_{context}_{epoch}", on_select="rerun",
        selection_mode=("points", "box", "lasso"),
    )
    points = event["selection"]["points"]
    if points:
        st.markdown("[View selected incident records](#selected-chart-incidents)")
    return points


def render_chart_records(frame, selections):
    st.subheader("Selected chart incidents")
    if not any(selections.values()):
        st.info("Click a chart bar or a weekly trend point to display its incidents here. Use Shift+click to select multiple points.")
        return
    selected, descriptions = filter_chart_incidents(frame, selections)
    st.text("; ".join(descriptions))
    if st.button("Clear chart selections", key="clear_chart_selections"):
        # Plotly widget selection state is read-only; a fresh key clears it.
        st.session_state["chart_selection_epoch"] = st.session_state.get("chart_selection_epoch", 0) + 1
        st.session_state.pop("chart_record_search", None)
        st.rerun()
    query = st.text_input("Search selected incidents", placeholder="Search within the chart selection", key="chart_record_search")
    records = search_incidents(selected, query).sort_values("opened_at", ascending=False, kind="stable")
    st.caption(f"{len(records):,} matching incidents · {len(selected):,} in the chart selection · {len(frame):,} in the sidebar scope")
    st.download_button(
        "Download selected incidents (CSV)", csv_bytes(records), file_name="chart_selected_incidents.csv",
        mime="text/csv", disabled=records.empty, key="download_chart_records",
    )
    if records.empty:
        st.info("No incidents match these combined chart selections and search. Clear the selections or broaden your search.")
    incident_table(records)


def render_sla_settings(severities):
    if "sla_policy" not in st.session_state:
        st.session_state["sla_policy"] = validate_sla_policy(DEFAULT_SLA_POLICY)
    with st.expander("SLA settings"):
        st.caption(
            "Define acknowledgement and resolution targets in minutes for each severity. "
            "Starting values are illustrative examples; review them for your service agreements."
        )
        imported = st.file_uploader("Import SLA settings (JSON)", type=["json"], key="sla_import")
        if st.button("Import SLA settings", disabled=imported is None):
            try:
                policy = validate_sla_policy(json.loads(imported.getvalue()))
            except (ValueError, UnicodeDecodeError) as exc:
                st.error(f"Cannot import SLA settings: {exc}")
            else:
                st.session_state["sla_policy"] = policy
                for key in list(st.session_state):
                    if key.startswith(("sla_enabled_", "sla_ack_", "sla_resolve_")):
                        st.session_state.pop(key)
                st.success("SLA settings imported and applied.")
        policy = st.session_state["sla_policy"]
        names = severity_order(set(severities) | set(DEFAULT_SLA_POLICY) | set(policy))
        with st.form("sla_policy_form"):
            draft = {}
            for severity in names:
                targets = policy.get(severity, DEFAULT_SLA_POLICY.get(severity, {"ack_minutes": 15.0, "resolve_minutes": 60.0}))
                enabled = st.checkbox(f"Enable {severity} SLA", value=severity in policy, key=f"sla_enabled_{severity}")
                ack = st.number_input(
                    f"{severity} acknowledgement (min)", min_value=0.0,
                    value=targets["ack_minutes"], step=1.0, key=f"sla_ack_{severity}",
                )
                resolve = st.number_input(
                    f"{severity} resolution (min)", min_value=0.0,
                    value=targets["resolve_minutes"], step=1.0, key=f"sla_resolve_{severity}",
                )
                if enabled:
                    draft[severity] = {"ack_minutes": ack, "resolve_minutes": resolve}
            if st.form_submit_button("Apply SLAs", width="stretch"):
                try:
                    st.session_state["sla_policy"] = validate_sla_policy(draft)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.success("SLA settings applied to the dashboard and executive reports.")
        st.download_button(
            "Download SLA settings", json.dumps(st.session_state["sla_policy"], indent=2),
            file_name="sla_settings.json", mime="application/json", key="download_sla_policy", width="stretch",
        )
        st.caption(
            "Settings stay active for this session, including CSV changes and filter resets. "
            "Download and import them to reuse them in a later session. "
            "Disable a severity to mark its incidents Not configured."
        )
    return st.session_state["sla_policy"]


def incident_table(frame):
    styled = frame.style.apply(highlight_sla_row, axis=1)
    # Keep row styling available for larger uploaded logs as well as the sample.
    with pd.option_context("styler.render.max_elements", max(262144, frame.size)):
        _incident_table(styled)


def _incident_table(styled):
    st.dataframe(
        styled, hide_index=True, width="stretch", height=460,
        column_order=[
            "incident_id", "severity", "sla_status", "service", "opened_at",
            "ack_minutes", "ack_sla_minutes", "ack_sla_status", "ack_overdue_minutes",
            "resolve_minutes", "resolve_sla_minutes", "resolve_sla_status", "resolve_overdue_minutes",
            "team", "region", "status", "summary",
        ],
        column_config={
            "incident_id": st.column_config.TextColumn("Incident ID"),
            "opened_at": st.column_config.DatetimeColumn("Opened at (UTC)", format="YYYY-MM-DD HH:mm"),
            "service": "Service", "team": "Team", "severity": "Severity", "region": "Region",
            "ack_minutes": st.column_config.NumberColumn("Ack (min)", format="plain"),
            "resolve_minutes": st.column_config.NumberColumn("Resolve (min)", format="plain"),
            "status": "Status", "summary": st.column_config.TextColumn("Summary", width="large"),
            "sla_status": st.column_config.TextColumn("SLA status", width="small"),
            "ack_sla_minutes": st.column_config.NumberColumn("Ack SLA (min)", format="plain"),
            "resolve_sla_minutes": st.column_config.NumberColumn("Resolve SLA (min)", format="plain"),
            "ack_sla_status": "Ack SLA status", "resolve_sla_status": "Resolve SLA status",
            "ack_overdue_minutes": st.column_config.NumberColumn("Ack overdue (min)", format="plain"),
            "resolve_overdue_minutes": st.column_config.NumberColumn("Resolve overdue (min)", format="plain"),
        },
    )


def render_sla_performance(frame, context):
    st.subheader("SLA performance")
    metrics = calculate_sla_metrics(frame)
    one, two, three, four = st.columns(4)
    one.metric("SLA breached incidents", f"{metrics.breached:,}", help="Unique incidents breaching acknowledgement, resolution, or both targets.")
    two.metric("Ack SLA breaches", f"{metrics.ack_breached:,}")
    three.metric("Resolution SLA breaches", f"{metrics.resolve_breached:,}")
    four.metric("SLA unknown / no policy", f"{metrics.unknown:,} / {metrics.not_configured:,}")
    st.caption(
        f"{metrics.met:,} incidents met both targets. Counts use the active severity-based SLA settings. "
        "A confirmed breach takes priority over an unknown duration. The 15-minute acknowledgement KPI above is separate."
    )
    if frame.empty:
        return []
    with st.container(border=True):
        st.markdown("**SLA outcomes by severity**")
        counts = frame.groupby(["severity", "sla_status"]).size().reset_index(name="incidents")
        figure = px.bar(
            counts, x="severity", y="incidents", color="sla_status", text="incidents",
            color_discrete_map=SLA_COLORS,
            category_orders={"severity": severity_order(frame["severity"].unique()), "sla_status": list(SLA_STATUSES)},
            labels={"severity": "Severity", "incidents": "Incidents", "sla_status": "SLA outcome"},
            custom_data=["severity", "sla_status"],
        )
        polish(figure, height=300)
        figure.update_layout(showlegend=True, legend=dict(orientation="h", y=1.15, x=0), barmode="stack")
        return selectable_chart(figure, "sla", context)


def render_overview(frame, context):
    st.caption("Click chart bars or points to inspect incidents below. Selections from different charts combine; sidebar filters continue to apply.")
    selections = {"sla": render_sla_performance(frame, context)}
    if frame.empty:
        st.info("No incidents match the dashboard filters. Expand the date range or select more filter values.")
        return
    left, right = st.columns(2, gap="large")
    with left, st.container(border=True):
        st.subheader("Incidents by severity")
        counts = frame["severity"].value_counts().rename_axis("severity").reset_index(name="incidents")
        figure = px.bar(
            counts, x="severity", y="incidents", color="severity", text="incidents",
            color_discrete_map=SEVERITY_COLORS,
            category_orders={"severity": severity_order(counts["severity"])},
            labels={"severity": "Severity", "incidents": "Incidents"},
            custom_data=["severity"],
        )
        figure.update_traces(textposition="outside", cliponaxis=False)
        figure.update_yaxes(dtick=1 if counts["incidents"].max() <= 10 else None)
        selections["severity"] = selectable_chart(polish(figure), "severity", context)
    with right, st.container(border=True):
        st.subheader("Incidents by service")
        counts = frame.groupby("service").size().reset_index(name="incidents")
        counts = counts.sort_values(["incidents", "service"], ascending=[True, False])
        figure = px.bar(
            counts, x="incidents", y="service", orientation="h", text="incidents",
            color_discrete_sequence=["#2563EB"],
            labels={"service": "", "incidents": "Incidents"},
            custom_data=["service"],
        )
        figure.update_yaxes(categoryorder="array", categoryarray=counts["service"].tolist())
        figure.update_traces(textposition="outside", cliponaxis=False)
        selections["service"] = selectable_chart(polish(figure), "service", context)
    left, right = st.columns(2, gap="large")
    with left, st.container(border=True):
        st.subheader("Weekly MTTR trend")
        st.caption("Mean resolution minutes, grouped by the week each incident opened. Weeks start Monday.")
        if frame["resolve_minutes"].notna().any():
            trend = weekly_mttr(frame)
            trend["week_id"] = trend["week"].map(lambda week: week.isoformat())
            figure = px.line(
                trend, x="week", y="mttr", markers=True, custom_data=["week_id", "incidents"],
                color_discrete_sequence=["#2563EB"],
                labels={"week": "Week starting (UTC)", "mttr": "MTTR (min)"},
            )
            figure.update_traces(
                connectgaps=False, line=dict(width=3),
                hovertemplate="Week of %{x|%b %d}<br>MTTR: %{y:,.1f} min<br>Incidents: %{customdata[1]}<extra></extra>",
            )
            selections["week"] = selectable_chart(polish(figure), "week", context)
        else:
            st.info("No valid resolution durations in this selection.")
    with right, st.container(border=True):
        st.subheader("Acknowledgement time distribution")
        st.caption("The reference line marks the 15-minute acknowledgement threshold.")
        known = frame.dropna(subset=["ack_minutes"])
        if not known.empty:
            bins = acknowledgement_bins(known)
            figure = px.bar(
                bins, x="center", y="incidents", color_discrete_sequence=["#5C85D6"],
                labels={"center": "Acknowledgement (min)", "incidents": "Incidents"},
                custom_data=["bin_start", "bin_end", "include_right"],
            )
            figure.update_traces(
                width=bins["bin_end"] - bins["bin_start"],
                hovertemplate="Ack: %{customdata[0]:.3g}–%{customdata[1]:.3g} min<br>Incidents: %{y}<extra></extra>",
            )
            figure.update_layout(bargap=0.04)
            figure.add_vline(x=15, line_dash="dash", line_color="#D97706", annotation_text="15 min")
            figure.update_yaxes(title="Incidents")
            selections["ack"] = selectable_chart(polish(figure), "ack", context)
        else:
            st.info("No valid acknowledgement durations in this selection.")
    with st.container(border=True):
        st.subheader("Top 5 noisiest services")
        st.caption("Ranked by incident count in the filtered view. Ties are ordered by service name.")
        top = frame.groupby("service").size().reset_index(name="incidents")
        top = top.sort_values(["incidents", "service"], ascending=[False, True]).head(5)
        top.insert(0, "rank", range(1, len(top) + 1))
        top["share"] = top["incidents"] / len(frame) * 100
        st.dataframe(
            top, hide_index=True, width="stretch",
            column_config={
                "rank": "Rank", "service": "Service", "incidents": "Incidents",
                "share": st.column_config.ProgressColumn(
                    "Share of incidents", min_value=0, max_value=100, format="%.1f%%",
                ),
            },
        )
    render_chart_records(frame, selections)


def render_explorer(frame):
    st.subheader("Incident explorer")
    st.caption("Search any field. Click a column header to sort. Breached rows are red; unknown SLA outcomes are amber.")
    query = st.text_input("Search incidents", placeholder="Incident ID, summary, service, region, or status", key="search")
    searched = search_incidents(frame, query)
    breaches_only = st.checkbox("Show SLA breaches only", key="breaches_only")
    if breaches_only:
        searched = searched.loc[searched["sla_status"].eq("Breached")]
    left, right = st.columns([3, 1])
    with left:
        st.caption(f"{len(searched):,} of {len(frame):,} filtered incidents")
    with right:
        st.download_button(
            "Download matching CSV", data=csv_bytes(searched), file_name="filtered_incidents.csv",
            mime="text/csv", width="stretch", disabled=searched.empty, key="download_incidents",
        )
    if searched.empty:
        st.info("No incidents match this search and the sidebar filters.")
    incident_table(searched.sort_values("opened_at", ascending=False, kind="stable"))


def render_report(frame, scoped, *, services, severities, teams, source, dataset_key, sla_policy):
    st.subheader("Executive report")
    st.caption(
        "The report period replaces the sidebar date range. Sidebar severity, service, and team filters still apply. "
        "Reports use the full loaded file within that scope."
    )
    first, second, third = st.columns(3)
    with first:
        period = st.selectbox("Report period", PERIODS, key="report_period")
    with second:
        selected_service = st.selectbox(
            "Report service", [None] + services,
            format_func=lambda service: "All selected services" if service is None else service,
            key=f"report_service_{dataset_key}",
        )
    with third:
        anchor = st.selectbox(
            "Report ends at", ["Current time (UTC)", "Latest incident in file"],
            key="report_anchor",
        )
    if anchor == "Current time (UTC)":
        end = pd.Timestamp.now(tz="UTC")
    else:
        end = frame["opened_at"].max()
        st.info(f"Historical report: periods end at the latest incident, {end:%Y-%m-%d %H:%M UTC}.")
    report_services = [selected_service] if selected_service is not None else services
    scope = (
        f"Source: {source}; severities: {', '.join(severities) or 'none'}; "
        f"teams: {', '.join(teams) or 'none'}; time reference: {anchor}."
    )
    report = build_executive_report(scoped, services=report_services, period=period, end=end, scope=scope, sla_policy=sla_policy)
    st.caption(f"Reporting window: {report.start:%Y-%m-%d %H:%M UTC} to {report.end:%Y-%m-%d %H:%M UTC}")
    if not report.metrics.total and anchor == "Current time (UTC)":
        st.info(
            "No incidents match this rolling period. For historical files, select 'Latest incident in file' "
            "or choose a longer reporting period."
        )
    filename = f"incident_executive_report_{report.end:%Y%m%d}_{period.lower().replace(' ', '_')}"
    with st.container(border=True):
        st.markdown("**Export report**")
        one, two, three, four = st.columns(4)
        with one:
            st.download_button(
                "Download report (HTML)", report.html, file_name=f"{filename}.html",
                mime="text/html", width="stretch", key="download_html",
            )
        with two:
            st.download_button(
                "Download report (Markdown)", report.markdown, file_name=f"{filename}.md",
                mime="text/markdown", width="stretch", key="download_markdown",
            )
        with three:
            st.download_button(
                "Download service metrics (CSV)", csv_bytes(report.services),
                file_name=f"{filename}_services.csv", mime="text/csv", width="stretch", key="download_services",
            )
        with four:
            st.download_button(
                "Download SLA breaches (CSV)", csv_bytes(report.sla_breaches),
                file_name=f"{filename}_sla_breaches.csv", mime="text/csv", width="stretch", key="download_breaches",
            )
        st.caption("Open the HTML report in a browser to print or save it as PDF.")
    with st.container(border=True):
        st.markdown(report.markdown.replace("# Incident Executive Report\n", "", 1))


def main():
    st.markdown('<div class="eyebrow">OPERATIONS INTELLIGENCE</div>', unsafe_allow_html=True)
    st.title("Incident Log Analyzer")
    st.caption("Understand incident volume, response performance, and the services that need attention.")
    with st.sidebar:
        st.header("Data source")
        uploaded = st.file_uploader("Upload incident CSV", type=["csv"], help="Leave empty to use incident_log_sample.csv.")
    source = uploaded.name if uploaded is not None else "incident_log_sample.csv"
    try:
        content = uploaded.getvalue() if uploaded is not None else (BASE_DIR / source).read_bytes()
        frame, warnings = read_data(content)
    except FileNotFoundError:
        st.error("The sample CSV is missing. Upload an incident CSV using the sidebar.")
        st.stop()
    except (ValueError, OSError) as exc:
        st.error(str(exc))
        st.stop()
    if frame.empty:
        for warning in warnings:
            st.warning(warning)
        st.info("This file has no incidents with valid opened_at timestamps. Upload a file with incident data.")
        st.stop()
    dataset_key = sha256(content).hexdigest()[:12]
    min_date, max_date = frame["opened_at"].min().date(), frame["opened_at"].max().date()
    with st.sidebar:
        st.caption(f"{source} · {len(frame):,} incidents")
        st.caption(f"{min_date:%b %d, %Y} – {max_date:%b %d, %Y} · UTC")
        st.divider()
        st.header("Dashboard filters")
        dates = st.date_input(
            "Opened date range (UTC)", value=(min_date, max_date), min_value=min_date, max_value=max_date,
            key=f"dates_{dataset_key}",
        )
        severity_options = severity_order(frame["severity"].unique())
        service_options = sorted(frame["service"].unique())
        team_options = sorted(frame["team"].unique())
        severities = st.multiselect("Severity", severity_options, default=severity_options, key=f"severity_{dataset_key}")
        services = st.multiselect("Service", service_options, default=service_options, key=f"service_{dataset_key}")
        teams = st.multiselect("Team", team_options, default=team_options, key=f"team_{dataset_key}")
        if st.button("Reset filters", width="stretch"):
            for name in ("dates", "severity", "service", "team", "report_service"):
                st.session_state.pop(f"{name}_{dataset_key}", None)
            st.session_state.pop("search", None)
            st.session_state.pop("breaches_only", None)
            st.session_state.pop("chart_record_search", None)
            st.session_state["chart_selection_epoch"] = st.session_state.get("chart_selection_epoch", 0) + 1
            st.rerun()
        st.caption("An empty selection matches no incidents. Date bounds include the entire selected day.")
        st.divider()
        st.header("SLA policy")
        sla_policy = render_sla_settings(severity_options)
    frame = evaluate_slas(frame, sla_policy)
    if warnings:
        with st.expander(f"Data quality notes ({len(warnings)})"):
            for warning in warnings:
                st.warning(warning)
    if len(dates) == 2:
        date_range = dates
    else:
        st.info("Select an end date to complete the dashboard date range. Showing the selected start day for now.")
        date_range = (dates[0], dates[0]) if dates else (min_date, max_date)
    filtered = filter_incidents(
        frame, severities=severities, services=services, teams=teams, date_range=date_range,
    )
    scoped = filter_incidents(frame, severities=severities, services=services, teams=teams)
    chart_context = sha256(json.dumps({
        "dataset": dataset_key, "dates": [str(day) for day in date_range],
        "severities": severities, "services": services, "teams": teams, "sla_policy": sla_policy,
    }, sort_keys=True).encode()).hexdigest()[:16]
    metrics = calculate_metrics(filtered)
    st.caption(f"DASHBOARD VIEW · {len(filtered):,} of {len(frame):,} incidents · {date_range[0]:%b %d, %Y} – {date_range[1]:%b %d, %Y}")
    one, two, three, four = st.columns(4)
    one.metric("Total incidents", f"{metrics.total:,}")
    two.metric("MTTR", format_minutes(metrics.mttr), help="Mean of valid resolve_minutes values in this filtered view, regardless of status.")
    three.metric("SEV1 count", f"{metrics.sev1:,}")
    four.metric("Ack ≤ 15 min", format_percent(metrics.ack_percent), help="Percentage of all filtered incidents with ack_minutes ≤ 15. Unknown values do not meet the threshold.")
    overview, explorer, reports = st.tabs(["Overview", "Incident explorer", "Executive report"])
    with overview:
        render_overview(filtered, chart_context)
    with explorer:
        render_explorer(filtered)
    with reports:
        render_report(
            frame, scoped, services=services, severities=severities, teams=teams,
            source=source, dataset_key=dataset_key,
            sla_policy=sla_policy,
        )
    st.caption("All timestamps are UTC. MTTR excludes unknown durations; acknowledgement compliance counts unknown times as not meeting the threshold.")


if __name__ == "__main__":
    main()
