"""Map chart selections to incident records using values, not chart row indices."""

import math

import numpy as np
import pandas as pd


def acknowledgement_bins(frame: pd.DataFrame, bins=30) -> pd.DataFrame:
    values = frame["ack_minutes"].dropna().to_numpy()
    columns = ["bin_start", "bin_end", "center", "incidents", "include_right"]
    if not len(values):
        return pd.DataFrame(columns=columns)
    counts, edges = np.histogram(values, bins=bins)
    return pd.DataFrame({
        "bin_start": edges[:-1], "bin_end": edges[1:],
        "center": (edges[:-1] + edges[1:]) / 2, "incidents": counts,
        "include_right": [int(index == len(counts) - 1) for index in range(len(counts))],
    })


def _point_filter(frame, kind, point):
    custom = point.get("customdata", [])
    # Plotly 6 numeric metadata arrives as a JS typed array. JSON serializes it
    # as {"0": value, "1": value, ...} rather than an ordinary list.
    if isinstance(custom, dict) and set(custom) == {str(index) for index in range(len(custom))}:
        custom = [custom[str(index)] for index in range(len(custom))]
    if not isinstance(custom, (list, tuple)):
        raise ValueError("Missing chart point metadata")
    if kind in {"severity", "service"}:
        value = str(custom[0])
        return frame[kind].eq(value), f"{kind.title()}: {value}"
    if kind == "sla":
        severity, outcome = str(custom[0]), str(custom[1])
        return frame["severity"].eq(severity) & frame["sla_status"].eq(outcome), f"SLA: {severity} / {outcome}"
    if kind == "week":
        start = pd.Timestamp(custom[0])
        if pd.isna(start):
            raise ValueError("Invalid selected week")
        start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
        end = start + pd.Timedelta(days=7)
        return frame["opened_at"].ge(start) & frame["opened_at"].lt(end), f"Week starting {start:%Y-%m-%d} UTC"
    if kind == "ack":
        start, end = float(custom[0]), float(custom[1])
        include_right = float(custom[2])
        if not math.isfinite(start) or not math.isfinite(end) or start >= end or include_right not in {0, 1}:
            raise ValueError("Invalid acknowledgement bin")
        upper = frame["ack_minutes"].le(end) if include_right else frame["ack_minutes"].lt(end)
        mask = frame["ack_minutes"].ge(start) & upper
        boundary = "≤" if include_right else "<"
        return mask, f"Ack: {start:.12g} ≤ minutes {boundary} {end:.12g}"
    raise ValueError("Unknown chart selection")


def filter_chart_incidents(frame: pd.DataFrame, selections: dict) -> tuple[pd.DataFrame, list[str]]:
    """OR points within each chart; AND selections made in different charts."""
    combined = pd.Series(True, index=frame.index)
    descriptions = []
    for kind, points in selections.items():
        if not points:
            continue
        chart_mask = pd.Series(False, index=frame.index)
        labels = []
        for point in points:
            try:
                mask, label = _point_filter(frame, kind, point)
            except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError):
                continue
            chart_mask |= mask
            if label not in labels:
                labels.append(label)
        combined &= chart_mask
        descriptions.extend(labels or ["Selection unavailable; clear it and select a chart again."])
    return frame.loc[combined].copy(), descriptions
