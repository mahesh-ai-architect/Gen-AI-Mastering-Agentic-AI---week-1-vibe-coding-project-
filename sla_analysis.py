"""Severity-based SLA policies and snapshot evaluation of recorded durations."""

from dataclasses import dataclass
import math

import pandas as pd


# Illustrative starting values, not contractual or industry-standard SLAs.
DEFAULT_SLA_POLICY = {
    "SEV1": {"ack_minutes": 15.0, "resolve_minutes": 60.0},
    "SEV2": {"ack_minutes": 30.0, "resolve_minutes": 240.0},
    "SEV3": {"ack_minutes": 60.0, "resolve_minutes": 1440.0},
    "SEV4": {"ack_minutes": 240.0, "resolve_minutes": 4320.0},
}
SLA_STATUSES = ("Breached", "Met", "Unknown", "Not configured")
SLA_COLORS = {
    "Breached": "#DC3545", "Met": "#16805D",
    "Unknown": "#D97706", "Not configured": "#8294AE",
}


@dataclass(frozen=True)
class SLAMetrics:
    breached: int
    ack_breached: int
    resolve_breached: int
    met: int
    unknown: int
    not_configured: int


def severity_order(values) -> list[str]:
    def order(value):
        suffix = value.removeprefix("SEV")
        return (0, int(suffix), value) if value.startswith("SEV") and suffix.isdigit() else (1, 0, value)
    return sorted(values, key=order)


def validate_sla_policy(policy) -> dict[str, dict[str, float]]:
    if not isinstance(policy, dict):
        raise ValueError("SLA settings must be a JSON object keyed by severity.")
    normalized = {}
    for severity, targets in policy.items():
        if not isinstance(severity, str) or not severity.strip():
            raise ValueError("Each SLA must have a nonempty severity name.")
        name = severity.strip().upper()
        if name in normalized:
            raise ValueError(f"Duplicate SLA severity: {name}.")
        if not isinstance(targets, dict) or set(targets) != {"ack_minutes", "resolve_minutes"}:
            raise ValueError(f"{name} must define ack_minutes and resolve_minutes.")
        normalized[name] = {}
        for field, value in targets.items():
            try:
                numeric = float(value)
            except (ValueError, TypeError, OverflowError):
                numeric = float("nan")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(numeric) or numeric < 0:
                raise ValueError(f"{name} {field} must be a finite, nonnegative number of minutes.")
            normalized[name][field] = numeric
    return normalized


def evaluate_slas(frame: pd.DataFrame, policy: dict) -> pd.DataFrame:
    """A known breach wins over an unknown duration; equality meets the SLA."""
    policy = validate_sla_policy(policy)
    result = frame.copy()
    for prefix, field in (("ack", "ack_minutes"), ("resolve", "resolve_minutes")):
        target = result["severity"].map({severity: values[field] for severity, values in policy.items()}).astype(float)
        values = result[field]
        known = values.notna() & values.ge(0) & values.map(math.isfinite)
        configured = target.notna()
        status = pd.Series("Not configured", index=result.index, dtype="string")
        status.loc[configured & ~known] = "Unknown"
        status.loc[configured & known] = "Met"
        status.loc[configured & known & values.gt(target)] = "Breached"
        result[f"{prefix}_sla_minutes"] = target
        result[f"{prefix}_sla_status"] = status
        result[f"{prefix}_overdue_minutes"] = (values - target).clip(lower=0).where(configured & known)
    result["sla_status"] = pd.Series("Not configured", index=result.index, dtype="string")
    configured = result["ack_sla_minutes"].notna()
    result.loc[configured, "sla_status"] = "Unknown"
    result.loc[result["ack_sla_status"].eq("Met") & result["resolve_sla_status"].eq("Met"), "sla_status"] = "Met"
    result.loc[result["ack_sla_status"].eq("Breached") | result["resolve_sla_status"].eq("Breached"), "sla_status"] = "Breached"
    return result


def calculate_sla_metrics(frame: pd.DataFrame) -> SLAMetrics:
    return SLAMetrics(
        breached=int(frame["sla_status"].eq("Breached").sum()),
        ack_breached=int(frame["ack_sla_status"].eq("Breached").sum()),
        resolve_breached=int(frame["resolve_sla_status"].eq("Breached").sum()),
        met=int(frame["sla_status"].eq("Met").sum()),
        unknown=int(frame["sla_status"].eq("Unknown").sum()),
        not_configured=int(frame["sla_status"].eq("Not configured").sum()),
    )


def sla_by_severity(frame: pd.DataFrame, policy: dict) -> pd.DataFrame:
    rows = []
    for severity in severity_order(set(frame["severity"]) | set(policy)):
        group = frame.loc[frame["severity"].eq(severity)]
        metrics = calculate_sla_metrics(group)
        targets = policy.get(severity, {})
        rows.append({
            "severity": severity, "ack_target": targets.get("ack_minutes"),
            "resolve_target": targets.get("resolve_minutes"), "incidents": len(group),
            "breached": metrics.breached, "ack_breached": metrics.ack_breached,
            "resolve_breached": metrics.resolve_breached, "met": metrics.met,
            "unknown": metrics.unknown, "not_configured": metrics.not_configured,
        })
    return pd.DataFrame(rows, columns=[
        "severity", "ack_target", "resolve_target", "incidents", "breached",
        "ack_breached", "resolve_breached", "met", "unknown", "not_configured",
    ])


def highlight_sla_row(row):
    """Highlight confirmed breaches and keep unknown outcomes visually distinct."""
    styles = []
    for column in row.index:
        style = "background-color: #FDE8E8; color: #991B1B" if row["sla_status"] == "Breached" else ""
        if column.endswith("sla_status"):
            if row[column] == "Breached":
                style = "background-color: #FECACA; color: #991B1B; font-weight: bold"
            elif row[column] == "Met":
                style = "background-color: #DCFCE7; color: #166534"
            elif row[column] == "Unknown":
                style = "background-color: #FEF3C7; color: #92400E"
        styles.append(style)
    return styles
