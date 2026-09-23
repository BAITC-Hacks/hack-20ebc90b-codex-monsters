"""Buyer UI against a real local HTTP server and isolated backend state.

These checks use the backend's labelled synthetic source and actual planning,
storage, approval and CSV routes. They never replace HttpClient with a mock or
inject a forecast provider. The unconnected forecast fallback is visibly DEMO;
successful integration is not evidence of forecast accuracy.
"""

from __future__ import annotations

import csv
from decimal import Decimal, ROUND_CEILING
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from apps.buyer_ui.client import ApiError, HttpClient


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "apps/buyer_ui/app.py"
BACKEND_AVAILABLE = all(
    importlib.util.find_spec(name) is not None
    for name in ("fastapi", "uvicorn", "pyarrow", "pydantic")
)
try:
    from streamlit.testing.v1 import AppTest
except ImportError:
    AppTest = None

SNAPSHOT_REQUEST = {
    "source_ids": ["synthetic-demo"],
    "mapping_version": "1.0",
    "mode": "synthetic_demo",
    "as_of": "2026-09-23T00:00:00+05:00",
}


class LiveBackend:
    """Uvicorn process with its own temporary data directory and captured logs."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.process = None
        self.log = None
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def start(self):
        env = dict(os.environ)
        env.pop("EKT_SOURCE_CONFIG", None)
        env.update(
            EKT_DATA_DIR=str(self.directory / "state"),
            EKT_DEMO_ACTOR="synthetic-ui-integration-buyer",
            EKT_DEMO_ROLE="approver",
            PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
        )
        self.log = (self.directory / "uvicorn.log").open("a", encoding="utf-8")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "ekt.api.app:app", "--host", "127.0.0.1",
             "--port", str(self.port), "--log-level", "warning"],
            cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 25
        health_client = HttpClient(self.url, timeout=0.5)
        while time.monotonic() < deadline and self.process.poll() is None:
            try:
                if health_client.health().get("status") == "ok":
                    return
            except ApiError:
                pass
            time.sleep(0.05)
        self.stop()
        raise AssertionError("Live API did not start:\n" +
                             (self.directory / "uvicorn.log").read_text(encoding="utf-8"))

    def stop(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None

    def restart(self):
        self.stop()
        self.start()


def completed(read, timeout=20):
    """Bounded observation of an accepted job; failed jobs are test failures."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = read()
        if result.get("status") == "succeeded":
            return result
        if result.get("status") == "failed":
            raise AssertionError(f"Live backend job failed: {result}")
        time.sleep(0.02)
    raise AssertionError(f"Live backend job did not finish within {timeout}s")


def larger_valid_quantity(line):
    # Test input selection only: the UI must never round a buyer's input.
    multiple = Decimal(line["pack_multiple_purchase"])
    minimum = max(Decimal(line["selected_purchase_qty"]) + multiple,
                  Decimal(line["moq_purchase"]))
    return str((minimum / multiple).to_integral_value(rounding=ROUND_CEILING) * multiple)


@unittest.skipUnless(BACKEND_AVAILABLE, "Install backend dependencies to run live HTTP integration")
class LiveBackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="buyer-ui-live-")
        self.addCleanup(self.temporary.cleanup)
        self.server = LiveBackend(Path(self.temporary.name))
        self.addCleanup(self.server.stop)
        self.server.start()
        self.client = HttpClient(self.server.url, timeout=5)

    def build_run(self):
        sources = self.client.list_sources()
        self.assertIn("synthetic-demo", [item["source_id"] for item in sources["items"]])
        accepted = self.client.create_snapshot(dict(SNAPSHOT_REQUEST))
        job = completed(lambda: self.client.get_job(accepted["job_id"]))
        snapshot_id = job["result_ref"]
        snapshot = self.client.get_snapshot(snapshot_id)
        self.assertEqual(snapshot["mode"], "synthetic_demo")
        self.assertTrue(snapshot["quality"]["capabilities"]["can_plan"])
        request = {
            "snapshot_id": snapshot_id,
            "policy": {"service_metric": "cycle_service", "service_target": 0.95,
                       "lead_time_delay_days": 0, "policy_version": "1.0"},
            "idempotency_key": "live-ui-baseline",
        }
        accepted = self.client.create_planning_run(request)
        run = completed(lambda: self.client.get_planning_run(accepted["run_id"]))
        page = self.client.list_proposals(run_id=run["id"])
        proposals = [self.client.get_proposal(item["proposal_id"]) for item in page["items"]]
        self.assertTrue(proposals)
        self.assertEqual(set(run["proposal_ids"]), {p["proposal_id"] for p in proposals})
        self.assertTrue(all(p["snapshot_id"] == snapshot_id for p in proposals))
        self.assertTrue(all(p["mode"] == "synthetic_demo" for p in proposals))
        # Repeated planning requests resolve to the existing persisted run.
        self.assertEqual(self.client.create_planning_run(request)["run_id"], run["id"])
        return snapshot, run, proposals

    def assert_api_status(self, status, action):
        with self.assertRaises(ApiError) as raised:
            action()
        self.assertEqual(raised.exception.status_code, status, str(raised.exception))
        self.assertTrue(raised.exception.code)

    def test_live_edit_approval_csv_and_restart_persistence(self):
        _, _, proposals = self.build_run()
        original = next(p for p in proposals if p["lines"] and p["capabilities"]["can_approve"])
        proposal_id = original["proposal_id"]
        line = original["lines"][0]
        self.assert_api_status(403, lambda: self.client.export_proposal(proposal_id, {
            "expected_version": original["version"], "idempotency_key": "draft-csv",
        }))
        self.assert_api_status(422, lambda: self.client.edit_proposal(proposal_id, {
            "expected_version": original["version"],
            "edits": [{"line_id": line["line_id"], "purchase_qty": "-1"}],
            "reason": "Synthetic invalid quantity check",
        }))
        self.assertEqual(self.client.get_proposal(proposal_id), original)
        patch_request = {
            "expected_version": original["version"],
            "edits": [{"line_id": line["line_id"], "purchase_qty": larger_valid_quantity(line)}],
            "reason": "Synthetic buyer override for integration verification",
        }
        edited = self.client.edit_proposal(proposal_id, patch_request)
        self.assertEqual(edited["version"], original["version"] + 1)
        self.assertEqual(edited["status"], "draft")
        changed_line = next(row for row in edited["lines"] if row["line_id"] == line["line_id"])
        self.assertEqual(changed_line["recommended_purchase_qty"], line["recommended_purchase_qty"])
        self.assertIn("manual_override_delta", [part["code"] for part in changed_line["explanation"]])
        self.assert_api_status(409, lambda: self.client.edit_proposal(proposal_id, patch_request))
        self.assert_api_status(409, lambda: self.client.approve_proposal(proposal_id, {
            "expected_version": original["version"], "content_hash": original["content_hash"],
        }))
        self.assert_api_status(409, lambda: self.client.approve_proposal(proposal_id, {
            "expected_version": edited["version"], "content_hash": original["content_hash"],
        }))
        approval = self.client.approve_proposal(proposal_id, {
            "expected_version": edited["version"], "content_hash": edited["content_hash"],
        })
        self.assertEqual(approval["status"], "approved")
        approved = HttpClient(self.server.url).get_proposal(proposal_id)
        self.assertEqual(approved["status"], "approved")
        self.assertTrue(approved["capabilities"]["can_export"])
        export_request = {"expected_version": edited["version"], "idempotency_key": "approved-csv"}
        exported = self.client.export_proposal(proposal_id, export_request)
        self.assertEqual(self.client.export_proposal(proposal_id, export_request), exported)
        csv_stream = io.StringIO(exported.data.decode("utf-8-sig"))
        self.assertEqual(next(csv.reader(csv_stream)), ["ДЕМОНСТРАЦИЯ — НЕ ЗАКАЗ ПОСТАВЩИКУ"])
        rows = list(csv.DictReader(csv_stream))
        self.assertEqual(len(rows), sum(Decimal(line["selected_purchase_qty"]) > 0 for line in edited["lines"]))
        self.assertTrue(all(row["mode"] == "synthetic_demo" and row["status"] == "approved" for row in rows))
        self.assertTrue(all(row["proposal_id"] == proposal_id and int(row["version"]) == edited["version"] for row in rows))
        self.server.restart()
        reopened = HttpClient(self.server.url)
        self.assertEqual(reopened.get_proposal(proposal_id), approved)
        self.assertEqual(reopened.export_proposal(proposal_id, export_request), exported)
        later = reopened.edit_proposal(proposal_id, {
            **patch_request, "expected_version": edited["version"],
            "edits": [{"line_id": changed_line["line_id"], "purchase_qty": larger_valid_quantity(changed_line)}],
        })
        self.assertEqual(later["status"], "draft")
        self.assert_api_status(409, lambda: reopened.export_proposal(proposal_id, export_request))
        self.assert_api_status(403, lambda: reopened.export_proposal(proposal_id, {
            "expected_version": later["version"], "idempotency_key": "unapproved-new-version",
        }))

    def test_live_scenario_retains_original_run_and_discloses_forecast_source(self):
        _, run, proposals = self.build_run()
        before = {p["proposal_id"]: p for p in proposals}
        events = self.client.list_demand_events(run["id"], limit=25)
        self.assertIn("items", events)
        fallback = any(w["code"] == "FORECAST_PROVIDER_NOT_CONNECTED"
                       for p in proposals for line in p["lines"] for w in line["warnings"])
        if fallback:
            self.assertEqual(events["items"], [], "Synthetic scaffold must not invent classification")
        request = {"base_run_id": run["id"], "overrides": {"service_target": 0.99,
                   "lead_time_delay_days": 7}, "seed": 42, "idempotency_key": "live-scenario"}
        accepted = self.client.create_scenario(request)
        scenario = completed(lambda: self.client.get_scenario(accepted["scenario_id"]))
        self.assertEqual(scenario["base_run_id"], run["id"])
        self.assertTrue(scenario["changed_lines"])
        self.assertTrue(scenario["assumptions"])
        self.assertEqual(Decimal(scenario["summary"]["baseline_total_cost"]),
                         sum(Decimal(p["total_cost"]) for p in proposals))
        self.assertEqual(self.client.create_scenario(request)["scenario_id"], scenario["id"])
        self.assertEqual({p["proposal_id"] for p in self.client.list_proposals()["items"]}, set(before))
        for proposal_id, original in before.items():
            self.assertEqual(self.client.get_proposal(proposal_id), original)

    @unittest.skipUnless(AppTest is not None, "Streamlit is needed for live UI transition checks")
    def test_normal_buyer_wizard_then_browser_reload_restores_approved_order(self):
        with patch.dict(os.environ, {
            "BUYER_UI_MODE": "http", "BUYER_DEVELOPER_MODE": "0", "BUYER_API_URL": self.server.url,
            "BUYER_API_TOKEN": "", "BUYER_SNAPSHOT_ID": "", "BUYER_RUN_ID": "",
        }):
            app = AppTest.from_file(str(APP), default_timeout=20).run()

            def clean():
                self.assertFalse(list(app.exception), [str(error.value) for error in app.exception])
                self.assertFalse(list(app.code))
                self.assertFalse(list(app.json))

            def click(label):
                next(button for button in app.button if button.label == label).click().run()
                clean()

            click("Перейти к данным")
            self.assertEqual(app.selectbox(key="buyer_source_group").options, ["Демонстрационный набор"])
            self.assertFalse(any(item.label in {"Адрес API", "ID поставщика", "Данные на дату и время", "Номер набора данных"} for item in app.text_input))
            click("Подготовить план закупки")
            deadline = time.monotonic() + 20
            while "buyer_flow" in app.session_state and time.monotonic() < deadline:
                time.sleep(0.05)
                app.run()
                clean()
            self.assertNotIn("buyer_flow", app.session_state)
            run_id = app.session_state["run_id"]
            self.assertEqual(app.radio(key="workspace_page").value, "Заказы")
            proposal_id = app.session_state["proposal_id"]
            click("Утвердить и подготовить CSV")
            self.assertEqual(self.client.get_proposal(proposal_id)["status"], "approved")
            self.assertIn("order_download", app.session_state)
            # No configured IDs or reused session state: persistent workspace owns recovery.
            fresh = AppTest.from_file(str(APP), default_timeout=20).run()
            self.assertFalse(list(fresh.exception), [str(error.value) for error in fresh.exception])
            self.assertEqual(fresh.session_state["run_id"], run_id)
            self.assertEqual(fresh.session_state["snapshot_id"], app.session_state["snapshot_id"])
            self.assertTrue(any(button.label == "Подготовить CSV" and not button.disabled for button in fresh.button))
            app.radio(key="workspace_page").set_value("Что, если…").run()
            clean()
            click("Сравнить варианты")
            scenario_id = app.session_state["_secondary_scenario"]["id"]
            completed(lambda: self.client.get_scenario(scenario_id))
            click("Обновить результат")
            click("Применить условия к новому плану")
            deadline = time.monotonic() + 20
            while "buyer_flow" in app.session_state and time.monotonic() < deadline:
                time.sleep(0.05)
                app.run()
                clean()
            self.assertNotIn("buyer_flow", app.session_state)
            new_run_id = app.session_state["run_id"]
            self.assertNotEqual(new_run_id, run_id)
            self.assertEqual(self.client.get_planning_run(new_run_id)["policy"]["service_target"], 0.99)
            self.assertEqual(self.client.get_proposal(proposal_id)["status"], "approved")
            for item in self.client.list_proposals(run_id=new_run_id)["items"]:
                self.assertEqual(item["status"], "draft")
            app.selectbox(key="saved_run_picker").set_value(run_id).run()
            click("Открыть выбранный план")
            self.assertEqual(app.session_state["run_id"], run_id)
            self.assertTrue(any(button.label == "Подготовить CSV" for button in app.button))

    @unittest.skipUnless(AppTest is not None, "Streamlit is needed for live UI transition checks")
    def test_streamlit_creates_snapshot_run_then_edits_approves_and_downloads_via_http(self):
        import streamlit as st

        original_export_option = st.get_option("client.disableDataExport")
        self.addCleanup(st.set_option, "client.disableDataExport", original_export_option)
        # Exercise a new server's default, not config left over by earlier AppTests.
        # The bootstrap must rerun before it exposes supplier tables.
        st.set_option("client.disableDataExport", False)
        with patch.dict(os.environ, {
            "BUYER_DEVELOPER_MODE": "1",
            "BUYER_UI_MODE": "http", "BUYER_API_URL": self.server.url,
            "BUYER_API_TOKEN": "", "BUYER_SNAPSHOT_ID": "", "BUYER_RUN_ID": "",
            "BUYER_MOCK_QUALITY": "ready", "BUYER_SEED": "42",
        }):
            app = AppTest.from_file(str(APP), default_timeout=20).run()

            def clean():
                self.assertFalse(list(app.exception), [str(error.value) for error in app.exception])
                self.assertFalse(list(app.json))
                self.assertFalse(list(app.code))
                self.assertFalse(any("для поддержки" in panel.label.casefold() for panel in app.expander))

            def click(label):
                choices = [button for button in app.button if button.label == label]
                self.assertEqual(len(choices), 1, label)
                self.assertFalse(choices[0].disabled, label)
                choices[0].click().run()
                clean()

            clean()
            self.assertTrue(st.get_option("client.disableDataExport"))
            self.assertIsInstance(app.session_state["client"], HttpClient)
            self.assertEqual(app.radio(key="workspace_page").options, ["План закупки", "Сравнение вариантов", "Данные"])
            click("Перейти к данным")
            app.multiselect(key="secondary_sources").set_value(["synthetic-demo"])
            app.text_input(key="secondary_as_of").set_value(SNAPSHOT_REQUEST["as_of"])
            click("Подготовить данные")
            job_id = app.session_state["_secondary_snapshot_job"]
            job = completed(lambda: self.client.get_job(job_id))
            click("Обновить статус импорта")
            click("Использовать эти данные")
            self.assertEqual(app.session_state["snapshot_id"], job["result_ref"])
            click("Рассчитать заказ")
            run_id = app.session_state["run_id"]
            completed(lambda: self.client.get_planning_run(run_id))
            click("Обновить статус расчёта")
            click("Открыть план закупки")
            proposal_id = app.session_state["proposal_id"]
            proposal = self.client.get_proposal(proposal_id)
            line_id = app.selectbox(key=f"line_picker:{proposal_id}").value
            line = next(row for row in proposal["lines"] if row["line_id"] == line_id)
            identity = f"{proposal_id}:{line_id}"
            app.text_input(key=f"qty:{identity}").set_value(larger_valid_quantity(line))
            app.text_area(key=f"reason:{identity}").set_value("Синтетический прогон покупателя через HTTP")
            click("Сохранить количество")
            edited = self.client.get_proposal(proposal_id)
            self.assertEqual(edited["version"], proposal["version"] + 1)
            click("Утвердить и подготовить CSV")
            self.assertEqual(self.client.get_proposal(proposal_id)["status"], "approved")
            download = app.session_state["order_download"]
            key = app.session_state["export_keys"][download["identity"]]
            actual = self.client.export_proposal(proposal_id, {
                "expected_version": edited["version"], "idempotency_key": key,
            })
            self.assertEqual(download["data"], actual.data)
            self.assertEqual(download["filename"], actual.filename)
            app.radio(key="workspace_page").set_value("Что, если…").run()
            clean()
            click("Сравнить варианты")
            scenario_id = app.session_state["_secondary_scenario"]["id"]
            scenario = completed(lambda: self.client.get_scenario(scenario_id))
            click("Обновить результат")
            self.assertEqual(self.client.get_proposal(proposal_id)["status"], "approved")
            rendered_rows = [row for table in app.dataframe
                             for row in table.value.to_dict("records")]
            cost_row = next(row for row in rendered_rows
                            if row.get("Показатель") == "Закупочная стоимость")
            self.assertEqual(Decimal(cost_row["База"].removesuffix(" KZT").replace("\u202f", "")),
                             Decimal(scenario["summary"]["baseline_total_cost"]))
            self.assertEqual(Decimal(cost_row["Сценарий"].removesuffix(" KZT").replace("\u202f", "")),
                             Decimal(scenario["summary"]["scenario_total_cost"]))
            selected_identity = json.loads(app.selectbox(
                key=f"secondary_scenario_line_{scenario_id}").value)
            compared = next(row for row in scenario["changed_lines"]
                            if [row["sku_id"], row["supplier_id"], row["warehouse_id"]]
                            == selected_identity)
            quantity_row = next(row for row in rendered_rows
                                if row.get("Показатель") == "Заказ в базовой единице")
            self.assertEqual(Decimal(quantity_row["База"].replace("\u202f", "")),
                             Decimal(compared["baseline_base_qty"]))
            self.assertEqual(Decimal(quantity_row["Сценарий"].replace("\u202f", "")),
                             Decimal(compared["scenario_base_qty"]))
            self.assertNotEqual(quantity_row["Единица"], "Не передана")
            self.assertTrue(any("Данные о страховом запасе для нового варианта отсутствуют" in item.value
                                for item in app.caption))
            # A fresh UI session recovers approval from HTTP, not previous session_state.
            with patch.dict(os.environ, {"BUYER_SNAPSHOT_ID": job["result_ref"], "BUYER_RUN_ID": run_id}):
                fresh = AppTest.from_file(str(APP), default_timeout=20).run()
                self.assertFalse(list(fresh.exception))
                self.assertIsNot(fresh.session_state["client"], app.session_state["client"])
                export_button = next(b for b in fresh.button if b.label == "Подготовить CSV")
                self.assertFalse(export_button.disabled)
                self.assertNotIn("order_download", fresh.session_state)


if __name__ == "__main__":
    unittest.main()
