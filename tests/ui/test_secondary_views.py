"""Critical secondary-tab transitions using explicit synthetic API responses."""

from copy import deepcopy
import unittest

from apps.buyer_ui.client import ApiError

try:
    from streamlit.testing.v1 import AppTest
except ImportError:
    AppTest = None


HARNESS = '''
import streamlit as st
from apps.buyer_ui.client import MockClient
from apps.buyer_ui.secondary_views import render_scenarios, render_data
if "client" not in st.session_state:
    st.session_state["client"] = MockClient()
defaults = dict(snapshot_id="demo-snapshot", run_id="demo-run", proposal_id="demo-proposal-tools",
                client_mode="mock", data_mode="synthetic_demo", base_seed=42,
                as_of="2026-09-01T00:00:00+00:00", mapping_version="demo-mapping-v1", policy_version="1.0")
for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = value
VIEW(st.session_state["client"])
'''


JOB_HARNESS = '''
import streamlit as st
from apps.buyer_ui.secondary_views import _show_job
_show_job(st.session_state["job"], "Подготовка данных")
'''


@unittest.skipUnless(AppTest is not None, "Streamlit required for job status tests")
class JobStatusTests(unittest.TestCase):
    def app(self, record):
        app = AppTest.from_string(JOB_HARNESS, default_timeout=15)
        app.session_state["job"] = record
        app.run()
        self.assertFalse(list(app.exception), [item.message for item in app.exception])
        return app

    def test_job_states_are_readable_without_json_and_show_only_active_progress(self):
        cases = (
            ("queued", "в очереди", 0.0, [0]),
            ("running", "выполняется", 0.35, [35]),
            ("succeeded", "готово", 1.0, []),
            ("failed", "не удалось завершить", 0.35, []),
        )
        for status, expected, progress, expected_bars in cases:
            with self.subTest(status=status):
                record = {"id": "synthetic-job", "status": status, "stage": "loading", "progress": progress,
                          "created_at": "2026-09-23T10:00:00+05:00", "updated_at": "2026-09-23T10:15:00+05:00"}
                if status == "failed":
                    record["error"] = {"code": "SOURCE_UNAVAILABLE", "message": "Источник данных недоступен."}
                app = self.app(record)
                text = "\n".join(str(item.value) for kind in
                                 ("markdown", "caption", "error", "success", "warning", "info")
                                 for item in getattr(app, kind))
                self.assertIn(expected, text.lower())
                self.assertFalse(list(app.json))
                self.assertIn("23.09.2026, 10:00 (UTC+05:00)", text)
                self.assertIn("23.09.2026, 10:15 (UTC+05:00)", text)
                self.assertEqual([bar.proto.value for bar in app.get("progress")], expected_bars)
                if status == "failed":
                    self.assertTrue(any("Источник данных недоступен." in item.value for item in app.error))
                else:
                    self.assertFalse(list(app.error))

    def test_missing_or_invalid_progress_does_not_become_a_completion_percentage(self):
        for progress in (None, -0.1, 1.1, "0.5", float("nan")):
            with self.subTest(progress=progress):
                app = self.app({"status": "running", "progress": progress})
                self.assertFalse(list(app.get("progress")))
                self.assertFalse(list(app.json))

    def test_timestamps_keep_the_supplied_clock_and_make_missing_information_explicit(self):
        from apps.buyer_ui.secondary_views import _format_timestamp

        cases = (
            (None, "Не указано"),
            ("", "Не указано"),
            ("not-a-date", "Дата не распознана"),
            ("2026-02-30T10:15:00Z", "Дата не распознана"),
            ("2026-09-23T10:15:00+05:00", "23.09.2026, 10:15 (UTC+05:00)"),
            ("2026-09-23T05:15:00Z", "23.09.2026, 05:15 (UTC+00:00)"),
            ("2026-09-23T01:45:00-03:30", "23.09.2026, 01:45 (UTC-03:30)"),
            ("2026-09-23T10:15:00", "23.09.2026, 10:15 (часовой пояс не указан)"),
            ("2026-09-23", "23.09.2026 (время не указано)"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(_format_timestamp(value), expected)


@unittest.skipUnless(AppTest is not None, "Streamlit required for UI transition tests")
class SecondaryViewsTests(unittest.TestCase):
    def app(self, view):
        app = AppTest.from_string(HARNESS.replace("VIEW(", view + "("), default_timeout=15).run()
        self.clean(app)
        return app

    def clean(self, app):
        self.assertFalse(list(app.exception), [item.message for item in app.exception])

    def submit(self, app, label):
        button = next(button for button in app.button if button.label == label)
        button.click().run()
        self.clean(app)

    def test_scenario_refresh_does_not_resubmit_or_mutate_base(self):
        app = self.app("render_scenarios")
        client = app.session_state["client"]
        base = client.get_proposal("demo-proposal-tools")
        self.submit(app, "Сравнить варианты")
        request = deepcopy(app.session_state["_secondary_scenario"]["request"])
        self.assertEqual(request["overrides"], {"service_target": 0.99, "lead_time_delay_days": 0})
        self.assertEqual(request["seed"], 42)
        self.submit(app, "Обновить результат")
        self.assertEqual(app.session_state["_secondary_scenario"]["request"], request)
        self.assertEqual(len(client._requests), 1)
        self.assertEqual(client.get_proposal("demo-proposal-tools"), base)
        self.assertFalse(any("Утвердить" in button.label or "CSV" in button.label for button in app.button))

    def test_ambiguous_scenario_retry_reuses_key(self):
        app = self.app("render_scenarios")
        client = app.session_state["client"]
        original = client.create_scenario
        attempts = []

        def timeout_once(payload):
            attempts.append(deepcopy(payload))
            response = original(payload)
            if len(attempts) == 1:
                raise ApiError(None, "TIMEOUT", "Ответ потерян", ambiguous=True, retryable=True)
            return response

        client.create_scenario = timeout_once
        self.submit(app, "Сравнить варианты")
        self.assertNotIn("_secondary_scenario", app.session_state)
        self.assertTrue(any("не получен" in item.value for item in app.warning))
        self.submit(app, "Сравнить варианты")
        self.assertEqual(attempts[0], attempts[1])
        self.assertIn("_secondary_scenario", app.session_state)
        self.assertEqual(len(client._requests), 1)

    def test_context_and_proposal_version_invalidate_scenario_display(self):
        app = self.app("render_scenarios")
        self.submit(app, "Сравнить варианты")
        client = app.session_state["client"]
        client.edit_proposal("demo-proposal-tools", {"expected_version": 1, "edits": [{"line_id": "line-tools", "purchase_qty": "120"}], "reason": "Новая версия"})
        app.run()
        self.clean(app)
        self.assertNotIn("_secondary_scenario", app.session_state)
        self.assertTrue(any("Версия базового предложения изменилась" in item.value for item in app.info))
        self.submit(app, "Сравнить варианты")
        app.session_state["snapshot_id"] = "another-snapshot"
        app.run()
        self.clean(app)
        self.assertNotIn("_secondary_scenario", app.session_state)
        self.assertTrue(any("другому набору данных" in item.value for item in app.error))

    def test_budget_disabled_without_top_level_capability(self):
        app = self.app("render_scenarios")
        client = app.session_state["client"]
        client._proposals["demo-proposal-tools"]["capabilities"]["budget_available"] = False
        app.run()
        self.clean(app)
        self.assertTrue(app.checkbox(key="secondary_budget_enabled").disabled)
        self.assertTrue(app.text_input(key="secondary_budget").disabled)

    def test_snapshot_timeout_cannot_silently_create_duplicate(self):
        app = self.app("render_data")
        calls = []

        def ambiguous(payload):
            calls.append(deepcopy(payload))
            raise ApiError(None, "TIMEOUT", "Ответ потерян", ambiguous=True, retryable=True)

        app.session_state["client"].create_snapshot = ambiguous
        app.multiselect(key="secondary_sources").set_value(["demo-source"])
        self.submit(app, "Подготовить данные")
        self.submit(app, "Подготовить данные")
        self.assertEqual(len(calls), 1)
        self.assertTrue(any("повтор не отправлен" in item.value for item in app.warning))
        self.assertEqual(app.session_state["snapshot_id"], "demo-snapshot")

    def test_snapshot_job_is_observed_without_recreating_snapshot(self):
        app = self.app("render_data")
        client = app.session_state["client"]
        original = client.create_snapshot
        calls = []

        def observe(payload):
            calls.append(deepcopy(payload))
            return original(payload)

        client.create_snapshot = observe
        app.multiselect(key="secondary_sources").set_value(["demo-source"])
        self.submit(app, "Подготовить данные")
        self.assertEqual(app.session_state["_secondary_snapshot_job"], "demo-snapshot-job")
        self.submit(app, "Обновить статус импорта")
        self.submit(app, "Подготовить данные")
        self.assertEqual(len(calls), 1)
        self.assertEqual(app.session_state["run_id"], "demo-run")

    def test_import_must_succeed_before_its_snapshot_can_be_selected(self):
        app = self.app("render_data")
        client = app.session_state["client"]
        job = client._fixture["snapshot_job"]
        app.session_state["snapshot_id"] = None
        app.session_state["_secondary_snapshot_job"] = "demo-snapshot-job"
        for status in ("queued", "running", "failed"):
            with self.subTest(status=status):
                job.update(status=status)
                if status == "failed":
                    job["error"] = {"code": "IMPORT_FAILED", "message": "Не удалось прочитать источник."}
                app.run()
                self.clean(app)
                self.assertFalse(any(button.key == "secondary_use_created_snapshot" for button in app.button))
                self.assertIsNone(app.session_state["snapshot_id"])
        job.update(status="succeeded", error=None)
        app.run()
        self.clean(app)
        self.submit(app, "Использовать эти данные")
        self.assertEqual(app.session_state["snapshot_id"], "demo-snapshot")
        self.assertIsNone(app.session_state["run_id"])
        self.assertTrue(any(button.label == "Рассчитать заказ" and not button.disabled for button in app.button))

    def test_project_pagination_resets_on_filter_change(self):
        app = self.app("render_data")
        client = app.session_state["client"]
        prototype = client._fixture["demand_events"]["items"][0]
        client._fixture["demand_events"]["items"] = [dict(deepcopy(prototype), event_id=f"event-{index}") for index in range(70)]
        app.run()
        self.submit(app, "Следующая страница событий")
        self.assertEqual(app.session_state["_secondary_events_cursors"], [None, "50"])
        app.selectbox(key="secondary_project_label").select("project").run()
        self.clean(app)
        self.assertEqual(app.session_state["_secondary_events_cursors"], [None])
        self.assertTrue(app.button(key="secondary_events_prev").disabled)

    def test_selecting_unknown_snapshot_retains_valid_context(self):
        app = self.app("render_data")
        app.text_input(key="secondary_snapshot_input").set_value("missing")
        self.submit(app, "Выбрать данные")
        self.assertEqual(app.session_state["snapshot_id"], "demo-snapshot")
        self.assertEqual(app.session_state["run_id"], "demo-run")
        self.assertTrue(any("не найден" in item.value for item in app.error))

    def test_failed_run_hides_prior_classification(self):
        app = self.app("render_data")
        client = app.session_state["client"]
        client._quality = "blocked"
        calls = []

        def unexpected_events(*args, **kwargs):
            calls.append((args, kwargs))
            return {"items": [], "next_cursor": None}

        client.list_demand_events = unexpected_events
        app.run()
        self.clean(app)
        self.assertEqual(calls, [])
        self.assertTrue(any("Классификация будет доступна" in item.value for item in app.info))

    def test_invalid_new_scenario_hides_previous_result(self):
        app = self.app("render_scenarios")
        self.submit(app, "Сравнить варианты")
        app.checkbox(key="secondary_budget_enabled").check()
        app.text_input(key="secondary_budget").set_value("NaN")
        self.submit(app, "Сравнить варианты")
        self.assertNotIn("_secondary_scenario", app.session_state)
        self.assertTrue(any("положительный конечный бюджет" in item.value for item in app.error))

    def test_backend_scenario_business_keys_and_baseline_cost_are_rendered(self):
        app = self.app("render_scenarios")
        client = app.session_state["client"]
        # Current public run schema omits policy/seed and may omit snapshot_id.
        for key in ("policy", "seed", "snapshot_id"):
            client._fixture["run"].pop(key, None)
        client._fixture["run"]["model_version"] = "fixture-v1"
        client._proposals["demo-proposal-tools"]["lines"][0]["warnings"].append({
            "code": "FORECAST_PROVIDER_NOT_CONNECTED", "severity": "warning",
            "message": "Прогноз fixture-v1 — временное синтетическое приближение.",
        })
        backend_result = deepcopy(client._fixture["scenarios"][0])
        for key in ("seed", "snapshot_id", "overrides", "warnings"):
            backend_result.pop(key, None)
        backend_result["summary"] = {
            "baseline_total_cost": "13800.00", "scenario_total_cost": "15000.00",
            "delta_cost": "1200.00", "currency": "KZT", "changed_line_count": 1,
        }
        backend_result["changed_lines"] = [
            {"sku_id": "TOOL-001", "supplier_id": "demo-supplier-tools", "warehouse_id": "demo-wh",
             "baseline_base_qty": "108", "scenario_base_qty": "120", "delta_base_qty": "12",
             "baseline_cost": "10800.00", "scenario_cost": "12000.00"},
            {"sku_id": "TOOL-001", "supplier_id": "demo-supplier-tools", "warehouse_id": "other-wh",
             "baseline_base_qty": "20", "scenario_base_qty": "20", "delta_base_qty": "0",
             "baseline_cost": "2000.00", "scenario_cost": "2000.00"},
        ]
        # Attach the actual proposal warehouse; identity must keep the second row distinct.
        backend_result["changed_lines"][0]["warehouse_id"] = client._proposals["demo-proposal-tools"]["warehouse_id"]
        client.get_scenario = lambda scenario_id: deepcopy(backend_result)
        self.submit(app, "Сравнить варианты")
        self.assertEqual(app.session_state["_secondary_scenario"]["request"]["seed"], 42)
        self.assertTrue(any("API не сообщает seed" in item.value for item in app.caption))
        self.assertTrue(any("fixture-v1" in item.value for item in app.json))
        self.assertTrue(any("временное синтетическое приближение" in item.value for item in app.warning))
        self.assertEqual(next(metric.value for metric in app.metric if metric.label == "Изменённых строк"), "1")
        selector = app.selectbox(key="secondary_scenario_line_demo-scenario-service")
        self.assertEqual(len(selector.options), 2)
        rows = [row for table in app.dataframe for row in table.value.to_dict("records")]
        self.assertIn({"Показатель": "Закупочная стоимость", "База": "13\u202f800 KZT", "Сценарий": "15\u202f000 KZT"}, rows)
        self.assertIn({"Показатель": "Заказ в базовой единице", "База": "108", "Сценарий": "120", "Единица": "шт"}, rows)
        self.assertTrue(any("Изменение количества: +12 шт" in item.value for item in app.caption))
        self.assertTrue(any("Данные о страховом запасе для нового варианта отсутствуют" in item.value for item in app.caption))

    def test_snapshot_job_uses_explicit_snapshot_id_without_result_ref(self):
        app = self.app("render_data")
        client = app.session_state["client"]
        client._fixture["snapshot_job"].update(snapshot_id="demo-snapshot", result_ref=None)
        app.session_state["_secondary_snapshot_job"] = "demo-snapshot-job"
        app.run()
        self.clean(app)
        self.assertFalse(app.button(key="secondary_use_created_snapshot").disabled)

    def test_snapshot_job_cannot_select_unexpected_snapshot(self):
        app = self.app("render_data")
        client = app.session_state["client"]
        client._fixture["snapshot_job"].update(snapshot_id="unexpected-snapshot", result_ref=None)
        original = client.get_snapshot
        client.get_snapshot = lambda snapshot_id: original("demo-snapshot")
        app.session_state["_secondary_snapshot_job"] = "demo-snapshot-job"
        app.run()
        self.clean(app)
        self.assertFalse(any(button.key == "secondary_use_created_snapshot" for button in app.button))
        self.assertTrue(any("другим ID" in item.value for item in app.error))
        self.assertEqual(app.session_state["snapshot_id"], "demo-snapshot")


if __name__ == "__main__":
    unittest.main()
