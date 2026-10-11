from pathlib import Path
from io import BytesIO
import json
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


APP = Path(__file__).resolve().parents[1] / "app.py"


def launch():
    return AppTest.from_file(str(APP), default_timeout=30).run()


def widget_by_label(widgets, label):
    return next(widget for widget in widgets if widget.label == label)


def select_chart_points(app, selections):
    # AppTest has no Plotly click control; send the same widget payload as the UI.
    states = app._tree.get_widget_states()
    for chart in app.get("plotly_chart"):
        key = chart.proto.id.split("-", 2)[-1]
        kind = key.split("_")[1]
        points = [{"customdata": data} for data in selections.get(kind, [])]
        states.widgets.add(id=chart.proto.id, string_value=json.dumps({
            "selection": {"points": points, "point_indices": list(range(len(points))), "box": [], "lasso": []},
        }))
    return app._run(states)


def test_default_dashboard_renders_all_requested_components():
    app = launch()
    assert not app.exception
    assert [metric.value for metric in app.metric][:4] == ["180", "1,123.2 min", "8", "10.0%"]
    assert len(app.get("plotly_chart")) == 5
    assert widget_by_label(app.metric, "SLA breached incidents").value == "105"
    assert len(app.dataframe[0].value) == 5
    assert len(app.dataframe[1].value) == 180
    assert len(app.get("download_button")) == 6
    assert "sla_status" in app.dataframe[1].value


def test_sidebar_filters_empty_state_and_reset():
    app = launch()
    widget_by_label(app.multiselect, "Severity").set_value(["SEV1"]).run()
    assert not app.exception
    assert app.metric[0].value == "8"
    assert app.dataframe[1].value["severity"].eq("SEV1").all()
    widget_by_label(app.multiselect, "Service").set_value([]).run()
    assert not app.exception
    assert app.metric[0].value == "0"
    assert app.metric[1].value == "N/A"
    widget_by_label(app.button, "Reset filters").click().run()
    assert not app.exception
    assert app.metric[0].value == "180"


def test_search_and_report_controls():
    app = launch()
    widget_by_label(app.text_input, "Search incidents").set_value("INC-1144").run()
    assert app.dataframe[1].value["incident_id"].tolist() == ["INC-1144"]
    widget_by_label(app.selectbox, "Report ends at").select("Latest incident in file").run()
    widget_by_label(app.selectbox, "Report period").select("Last week").run()
    widget_by_label(app.selectbox, "Report service").select("payments-api").run()
    assert not app.exception
    preview = "\n".join(element.value for element in app.markdown)
    assert "4 incidents across 1 service" in preview
    assert "2026-10-06 12:31 UTC" in preview


def test_report_period_replaces_dashboard_date_filter():
    app = launch()
    app.date_input[0].set_value(("2026-07-12", "2026-07-12")).run()
    widget_by_label(app.selectbox, "Report ends at").select("Latest incident in file").run()
    assert not app.exception
    assert app.metric[0].value == "1"
    preview = "\n".join(element.value for element in app.markdown)
    assert "3 incidents across 2 services" in preview


def test_invalid_csv_displays_error_instead_of_traceback():
    original = Path.read_bytes

    def read_bytes(path):
        return b"wrong,columns\n1,2\n" if path.name == "incident_log_sample.csv" else original(path)

    with patch.object(Path, "read_bytes", read_bytes):
        app = launch()
    assert not app.exception
    assert "Missing required columns" in app.error[0].value


def test_applying_sla_changes_updates_dashboard_table_and_report():
    app = launch()
    widget_by_label(app.multiselect, "Severity").set_value(["SEV1"]).run()
    widget_by_label(app.selectbox, "Report ends at").select("Latest incident in file").run()
    widget_by_label(app.selectbox, "Report period").select("Last month").run()
    assert widget_by_label(app.metric, "SLA breached incidents").value == "8"
    widget_by_label(app.number_input, "SEV1 resolution (min)").set_value(10000.0).run()
    assert widget_by_label(app.metric, "SLA breached incidents").value == "8"
    # Form values are sent together with submission, as they are in the browser.
    widget_by_label(app.number_input, "SEV1 resolution (min)").set_value(10000.0)
    widget_by_label(app.button, "Apply SLAs").click().run()
    assert not app.exception
    assert widget_by_label(app.metric, "SLA breached incidents").value == "0"
    assert app.dataframe[1].value["sla_status"].eq("Met").all()
    preview = "\n".join(element.value for element in app.markdown)
    assert "SLA breaches: 0 incidents" in preview
    assert "10,000 min" in preview
    widget_by_label(app.button, "Reset filters").click().run()
    assert app.session_state["sla_policy"]["SEV1"]["resolve_minutes"] == 10000.0


def test_breach_only_view_and_disabled_policies():
    app = launch()
    widget_by_label(app.checkbox, "Show SLA breaches only").check().run()
    assert not app.exception
    assert len(app.dataframe[1].value) == 105
    assert app.dataframe[1].value["sla_status"].eq("Breached").all()
    assert app.metric[0].value == "180"
    for severity in ("SEV1", "SEV2", "SEV3", "SEV4"):
        widget_by_label(app.checkbox, f"Enable {severity} SLA").uncheck()
    widget_by_label(app.button, "Apply SLAs").click().run()
    assert not app.exception
    assert app.dataframe[1].value.empty
    assert widget_by_label(app.metric, "SLA unknown / no policy").value == "0 / 180"


def test_sla_import_replaces_policy_and_updates_editor():
    policy = {"SEV1": {"ack_minutes": 22.0, "resolve_minutes": 300.0}}

    def uploader(label, **kwargs):
        return BytesIO(json.dumps(policy).encode()) if label == "Import SLA settings (JSON)" else None

    with patch("streamlit.file_uploader", side_effect=uploader):
        app = launch()
        widget_by_label(app.button, "Import SLA settings").click().run()
    assert not app.exception
    assert app.session_state["sla_policy"] == policy
    assert widget_by_label(app.number_input, "SEV1 acknowledgement (min)").value == 22.0
    assert widget_by_label(app.number_input, "SEV1 resolution (min)").value == 300.0
    assert not widget_by_label(app.checkbox, "Enable SEV2 SLA").value
    assert widget_by_label(app.metric, "SLA unknown / no policy").value == "0 / 172"


def test_invalid_sla_import_keeps_active_policy():
    def uploader(label, **kwargs):
        return BytesIO(b'{"SEV1": {"ack_minutes": -1, "resolve_minutes": 10}}') if label == "Import SLA settings (JSON)" else None

    with patch("streamlit.file_uploader", side_effect=uploader):
        app = launch()
        widget_by_label(app.button, "Import SLA settings").click().run()
    assert not app.exception
    assert "Cannot import SLA settings" in app.error[0].value
    assert app.session_state["sla_policy"]["SEV1"]["ack_minutes"] == 15.0


def test_chart_click_displays_matching_records_and_clear_restores_overview():
    app = launch()
    select_chart_points(app, {"severity": [["SEV1"]]})
    assert not app.exception
    assert len(app.dataframe) == 3
    selected = app.dataframe[1].value
    assert len(selected) == 8 and selected["severity"].eq("SEV1").all()
    assert len(app.dataframe[2].value) == 180
    assert app.metric[0].value == "180"
    assert len(app.get("download_button")) == 7
    widget_by_label(app.button, "Clear chart selections").click().run()
    assert not app.exception
    assert len(app.dataframe) == 2
    assert len(app.dataframe[1].value) == 180


def test_chart_selections_combine_and_search_within_selected_records():
    app = launch()
    select_chart_points(app, {"severity": [["SEV1"]], "service": [["inventory"]]})
    assert not app.exception
    selected = app.dataframe[1].value
    assert not selected.empty
    assert selected["severity"].eq("SEV1").all() and selected["service"].eq("inventory").all()
    incident = selected.iloc[0]["incident_id"]
    widget_by_label(app.text_input, "Search selected incidents").set_value(incident)
    select_chart_points(app, {"severity": [["SEV1"]], "service": [["inventory"]]})
    assert app.dataframe[1].value["incident_id"].tolist() == [incident]


def test_changing_sidebar_scope_resets_chart_selection():
    app = launch()
    select_chart_points(app, {"severity": [["SEV1"]]})
    old_keys = {chart.proto.id for chart in app.get("plotly_chart")}
    widget_by_label(app.multiselect, "Service").set_value(["checkout"]).run()
    assert not app.exception
    assert len(app.dataframe) == 2
    assert not old_keys.intersection(chart.proto.id for chart in app.get("plotly_chart"))


def test_changing_sla_policy_resets_chart_selection():
    app = launch()
    select_chart_points(app, {"sla": [["SEV1", "Breached"]]})
    assert app.dataframe[1].value["sla_status"].eq("Breached").all()
    widget_by_label(app.number_input, "SEV1 resolution (min)").set_value(10000.0)
    widget_by_label(app.button, "Apply SLAs").click().run()
    assert not app.exception
    assert len(app.dataframe) == 2


def test_chart_selection_does_not_change_executive_report_scope():
    app = launch()
    widget_by_label(app.selectbox, "Report ends at").select("Latest incident in file").run()
    select_chart_points(app, {"severity": [["SEV1"]]})
    assert not app.exception
    preview = "\n".join(element.value for element in app.markdown)
    assert "3 incidents across 2 services" in preview


def test_histogram_browser_payload_displays_exact_bin_records():
    app = launch()
    select_chart_points(app, {"ack": [{"0": 2.0, "1": 17.933333333333334, "2": 0.0}]})
    assert not app.exception
    selected = app.dataframe[1].value
    assert len(selected) == 23
    assert selected["ack_minutes"].ge(2).all() and selected["ack_minutes"].lt(17.933333333333334).all()
