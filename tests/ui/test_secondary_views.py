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
        self.submit(app, "Рассчитать сценарий")
        request = deepcopy(app.session_state["_secondary_scenario"]["request"])
        self.assertEqual(request["overrides"], {"service_target": 0.99, "lead_time_delay_days": 0})
        self.assertEqual(request["seed"], 42)
        self.submit(app, "Обновить статус сценария")
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
        self.submit(app, "Рассчитать сценарий")
        self.assertNotIn("_secondary_scenario", app.session_state)
        self.assertTrue(any("неизвестен" in item.value for item in app.warning))
        self.submit(app, "Рассчитать сценарий")
        self.assertEqual(attempts[0], attempts[1])
        self.assertIn("_secondary_scenario", app.session_state)
        self.assertEqual(len(client._requests), 1)

    def test_context_and_proposal_version_invalidate_scenario_display(self):
        app = self.app("render_scenarios")
        self.submit(app, "Рассчитать сценарий")
        client = app.session_state["client"]
        client.edit_proposal("demo-proposal-tools", {"expected_version": 1, "edits": [{"line_id": "line-tools", "purchase_qty": "120"}], "reason": "Новая версия"})
        app.run()
        self.clean(app)
        self.assertNotIn("_secondary_scenario", app.session_state)
        self.assertTrue(any("Версия базового предложения изменилась" in item.value for item in app.info))
        self.submit(app, "Рассчитать сценарий")
        app.session_state["snapshot_id"] = "another-snapshot"
        app.run()
        self.clean(app)
        self.assertNotIn("_secondary_scenario", app.session_state)
        self.assertTrue(any("другому snapshot" in item.value for item in app.error))

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
        self.submit(app, "Создать snapshot")
        self.submit(app, "Создать snapshot")
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
        self.submit(app, "Создать snapshot")
        self.assertEqual(app.session_state["_secondary_snapshot_job"], "demo-snapshot-job")
        self.submit(app, "Обновить статус импорта")
        self.submit(app, "Создать snapshot")
        self.assertEqual(len(calls), 1)
        self.assertEqual(app.session_state["run_id"], "demo-run")

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
        self.submit(app, "Проверить и выбрать snapshot")
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
        self.submit(app, "Рассчитать сценарий")
        app.checkbox(key="secondary_budget_enabled").check()
        app.text_input(key="secondary_budget").set_value("NaN")
        self.submit(app, "Рассчитать сценарий")
        self.assertNotIn("_secondary_scenario", app.session_state)
        self.assertTrue(any("положительный конечный бюджет" in item.value for item in app.error))


if __name__ == "__main__":
    unittest.main()
