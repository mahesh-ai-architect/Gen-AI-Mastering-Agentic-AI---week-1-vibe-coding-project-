# Incident Log Analyzer

A local Streamlit dashboard for incident volume, response performance, and executive reporting. The bundled `incident_log_sample.csv` loads automatically; upload a CSV in the sidebar to replace it. No API keys or external AI services are required.

## Run

Use Python 3.10 or newer. From this directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run app.py
```

Open the local URL shown by Streamlit, normally http://localhost:8501.

## Dashboard

- KPI cards show total incidents, MTTR in minutes, SEV1 count, and the percentage acknowledged within 15 minutes.
- Sidebar filters select an inclusive opened-date range, severity, service, and team. Empty multiselects match no incidents; **Reset filters** restores the full file.
- The Overview tab contains four incident charts, an SLA outcomes chart, SLA breach counts, and the five noisiest services.
- The Incident explorer searches all columns, supports sorting through column headers, and downloads matching incidents as CSV. Search affects the table and its export; dashboard KPIs and charts continue to use the sidebar filters.

### Click charts to inspect incidents

Click any severity bar, service bar, SLA outcome segment, weekly MTTR point, or acknowledgement histogram bar. Use **View selected incident records** beneath the selected chart to jump to the **Selected chart incidents** table, which shows the corresponding records, including their SLA status and targets. It supports search, column sorting, and a CSV download.

Use Shift+click, box selection, or lasso selection to select multiple points. Points within the same chart combine with OR; selections across different charts combine with AND. For example, select SEV1 and then an inventory service bar to see that service's SEV1 incidents. The selected scope is listed above the table. **Clear chart selections** restores the default view.

Chart selection narrows this detail table. The overview charts and KPIs retain their sidebar scope, the Incident explorer keeps its existing filters, and executive reports retain their selected reporting period and service scope. Changing sidebar filters, uploading a different file, applying a different SLA policy, or resetting filters clears chart selections.

Weekly selection includes incidents opened during the selected Monday–Sunday week. The acknowledgement histogram uses 30 explicit bins so a clicked bar maps to its exact incident records. Bins include their lower boundary and exclude the upper boundary, except the final bin, which includes the maximum value. Unknown acknowledgement durations are excluded from this histogram.

## SLA settings and breaches

Expand **SLA settings** under **SLA policy** in the sidebar. Set acknowledgement and resolution targets in minutes for each severity, then click **Apply SLAs**. Settings update the dashboard, incident table, and executive reports together. Uncheck a severity to disable its policy. Additional severities in an uploaded CSV appear in the editor with their policies disabled until you enable and apply them.

The app starts with these **illustrative example targets**, which should be adjusted to match your service agreements:

| Severity | Acknowledgement (min) | Resolution (min) |
| --- | ---: | ---: |
| SEV1 | 15 | 60 |
| SEV2 | 30 | 240 |
| SEV3 | 60 | 1440 |
| SEV4 | 240 | 4320 |

A duration **greater than** its target breaches the SLA; a duration exactly at the target meets it. An incident is counted once in the overall breach total even if both targets are breached. Separate acknowledgement and resolution breach totals can therefore overlap.

- **Breached:** at least one recorded duration exceeds its configured target.
- **Met:** both recorded durations are known and within their targets.
- **Unknown:** a configured target cannot be evaluated because a duration is missing or invalid, and no other target has a confirmed breach. A known breach takes priority over an unknown duration.
- **Not configured:** no active policy exists for the incident's severity.

The SLA chart shows these outcomes by severity. In the Incident explorer, confirmed breach rows are highlighted in red; unknown outcomes are amber. Columns show the applied targets, each target's outcome, and minutes overdue. **Show SLA breaches only** narrows the table and its CSV download without changing dashboard or report scope.

Policies remain active in the current session, including when the CSV changes or dashboard filters are reset. Use **Download SLA settings** and **Import SLA settings (JSON)** to reuse a policy in a later session. The JSON format maps severity names to numeric `ack_minutes` and `resolve_minutes` targets; an empty object disables all policies.

SLA evaluation compares recorded durations regardless of incident status. It does not estimate live elapsed time for unresolved incidents. The original fixed **Ack ≤ 15 min** KPI remains a separate benchmark and does not change with severity-specific SLA targets.

## Executive reports

Open the Executive report tab and select **Last 24 hours**, **Last week** (rolling 7 days), or **Last month** (one calendar month back). Choose an individual service or all services selected in the sidebar. Reports update automatically and include:

- An executive summary, incident-volume and MTTR comparisons with the preceding period.
- Per-service counts, MTTR, SEV1 count, acknowledgement compliance, and unresolved counts.
- SLA breach counts per service and severity, comparison with the prior period under the same policy, applied targets, and specific breached incidents with actual durations and minutes overdue.
- Repeated incident summaries, notable incidents, and evidence-based recommended actions.
- HTML and Markdown report downloads, a service-metrics CSV with SLA counts, and a CSV of all SLA breaches in the report scope. The report previews the 20 most recent breaches; the breach CSV contains the complete list. Open the standalone HTML report in a browser to print or save it as PDF.

The report period replaces the sidebar date range; severity, service, and team filters continue to apply. Table search does not affect reports. The reference time defaults to **Current time (UTC)**. For historical logs, choose **Latest incident in file**; this is clearly labeled in the report. The bundled sample contains 180 incidents from July 12 through October 6, 2026, so a report for the current last 24 hours can be empty.

The comparison window has exactly the same duration as the current window. Windows exclude their start and include their end, so a boundary incident is counted once and the latest incident is included.

The active SLA policy is applied consistently to both periods. Changing thresholds recalculates historical incidents; this is not an audit of which contractual policy was active when each incident occurred. Reports include the applied severity targets and unknown or unconfigured outcomes.

## CSV and metric definitions

Required columns:

```text
incident_id,opened_at,service,team,severity,region,ack_minutes,resolve_minutes,status,summary
```

Use a UTF-8 CSV with a header row; extra columns are ignored. All timestamps are shown and filtered in UTC. Timestamps without an offset are interpreted as UTC; explicit offsets are converted to UTC.

Invalid opened timestamps are excluded with a data quality note. Missing, nonnumeric, negative, or infinite timing values become unknown. MTTR is the mean of valid `resolve_minutes`, regardless of status, as requested; it is not total downtime. The acknowledgement KPI divides incidents with a known `ack_minutes <= 15` by **all incidents** in scope, so unknown values do not meet the threshold. An empty scope displays N/A for both metrics. Weekly MTTR groups by the week the incident opened, starting Monday, and leaves weeks with no valid timings as gaps.

Statuses other than `resolved` and `closed`, including `mitigated` or unknown, count as unresolved. Reports describe the current status of incidents opened in the selected period, not the entire older backlog or reconstructed historical status. Duplicate IDs are retained and flagged; counts are CSV row counts. Reports use deterministic analysis of the loaded data and do not infer customer impact or root causes.

## Verify

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Tests cover sample metrics, malformed data, date and timezone boundaries, search, rolling reports, missing values, report escaping, SLA boundary and overlap cases, policy validation and imports, chart selection and histogram boundaries, and Streamlit interactions.
