"""Streamlit critical-path checks using synthetic API state and injected failures."""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from apps.buyer_ui.client import ApiError

try:
    import streamlit as st
    from streamlit.testing.v1 import AppTest
except ImportError:
    AppTest = None


APP = Path(__file__).resolve().parents[2] / "apps/buyer_ui/app.py"
PROPOSAL = "demo-proposal-tools"
IDENTITY = f"{PROPOSAL}:line-tools"


@unittest.skipUnless(AppTest is not None, "Streamlit is needed for UI AppTest; transport tests still run")
class OrdersAppTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "BUYER_UI_MODE": "mock", "BUYER_MOCK_QUALITY": "ready",
            "BUYER_SNAPSHOT_ID": "demo-snapshot", "BUYER_RUN_ID": "demo-run", "BUYER_SEED": "42",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.app = AppTest.from_file(str(APP), default_timeout=15).run()
        self.assert_no_crash()

    def assert_no_crash(self):
        self.assertEqual(len(self.app.exception), 0, [error.value for error in self.app.exception])

    def button(self, label):
        matches = [button for button in self.app.button if button.label == label]
        self.assertEqual(len(matches), 1, f"Expected one button {label!r}")
        return matches[0]

    def client(self):
        return self.app.session_state["client"]

    def proposal(self):
        return self.client().get_proposal(PROPOSAL)

    def edit(self, quantity, reason="Подтверждённая потребность"):
        self.app.text_input(key=f"qty:{IDENTITY}").set_value(quantity)
        self.app.text_area(key=f"reason:{IDENTITY}").set_value(reason)
        self.button("Сохранить количество").click().run()
        self.assert_no_crash()

    def errors(self):
        # Human message is an error element; its protocol code lives in a caption.
        return "\n".join(str(item.value) for item in [*self.app.error, *self.app.caption])

    def test_task_navigation_and_explicit_mock_label(self):
        self.assertEqual(self.app.radio(key="workspace_page").options, ["План закупки", "Сравнение вариантов", "Данные"])
        self.assertTrue(any("Демо: имитация API" in str(item.value) for item in self.app.caption))
        self.assertFalse(any(b.label == "Подготовить CSV" for b in self.app.button))
        self.assertNotIn("order_download", self.app.session_state)
        self.assertEqual(self.app.selectbox(key=f"line_picker:{PROPOSAL}").value, "line-tools")

    def test_builtin_table_export_is_disabled_to_preserve_approval_gate(self):
        # Streamlit's dataframe menu must not bypass the approved API CSV flow.
        self.assertTrue(st.get_option("client.disableDataExport"))
        self.assertFalse(any(b.label == "Подготовить CSV" for b in self.app.button))
        self.assertNotIn("order_download", self.app.session_state)

    def test_selection_survives_sort_and_filter_changes_and_edits_by_line_id(self):
        client = self.client()
        original_get = client.get_proposal
        original_edit = client.edit_proposal
        requests = []
        shadow_sku = ["000-SHADOW"]

        def two_lines(proposal_id, version=None):
            proposal = original_get(proposal_id, version)
            if proposal_id == PROPOSAL:
                shadow = deepcopy(proposal["lines"][0])
                shadow.update(line_id="line-shadow", sku_id=shadow_sku[0], name="Other synthetic line", urgency="routine")
                proposal["lines"].append(shadow)
            return proposal

        def capture(proposal_id, payload):
            requests.append(deepcopy(payload))
            return original_edit(proposal_id, payload)

        client.get_proposal = two_lines
        client.edit_proposal = capture
        self.app.run()
        self.assert_no_crash()
        picker = f"line_picker:{PROPOSAL}"
        self.assertEqual(self.app.selectbox(key=picker).value, "line-tools")
        shadow_sku[0] = "ZZZ-SHADOW"
        self.app.run()
        self.app.selectbox(key="urgency_filter").select("soon").run()
        self.app.selectbox(key="urgency_filter").select("Все").run()
        self.assert_no_crash()
        self.assertEqual(self.app.selectbox(key=picker).value, "line-tools")
        self.edit("120", "Выбранная строка после изменения порядка")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["edits"], [{"line_id": "line-tools", "purchase_qty": "120"}])

    def test_empty_search_reset_restores_items_without_changing_order(self):
        original = self.proposal()
        self.app.text_input(key="order_search").set_value("Несуществующий артикул 999999")
        self.app.selectbox(key="urgency_filter").select("critical").run()
        self.assert_no_crash()
        self.assertTrue(any("Товары не найдены" in item.value for item in self.app.info))

        self.button("Сбросить поиск и фильтр").click().run()
        self.assert_no_crash()
        self.assertEqual(self.app.text_input(key="order_search").value, "")
        self.assertEqual(self.app.selectbox(key="urgency_filter").value, "Все")
        self.assertFalse(any("Товары не найдены" in item.value for item in self.app.info))
        self.assertEqual(self.app.selectbox(key=f"line_picker:{PROPOSAL}").value, "line-tools")
        self.assertEqual(self.proposal(), original)

    def test_edit_approve_refresh_export_then_edit_invalidates_download(self):
        self.edit("120")
        self.assertEqual(self.proposal()["version"], 2)
        self.assertEqual(self.proposal()["status"], "draft")
        self.assertFalse(any(b.label == "Подготовить CSV" for b in self.app.button))
        self.assertNotIn("order_download", self.app.session_state)
        self.app.button(key=f"approve:{PROPOSAL}").click().run()
        self.assert_no_crash()
        self.assertEqual(self.proposal()["status"], "approved")
        self.app.button(key="orders_refresh").click().run()
        self.assert_no_crash()
        self.assertEqual(self.proposal()["status"], "approved")
        exported = self.app.session_state["order_download"]
        self.assertEqual(exported["identity"][1], 2)
        self.assertIn("120", exported["data"].decode("utf-8-sig"))
        self.assertIn("НЕ ЗАКАЗ ПОСТАВЩИКУ", exported["data"].decode("utf-8-sig"))

        self.edit("132", "Дополнительная подтверждённая потребность")
        self.assertEqual(self.proposal()["version"], 3)
        self.assertEqual(self.proposal()["status"], "draft")
        self.assertNotIn("order_download", self.app.session_state)
        self.assertFalse(any(b.label == "Подготовить CSV" for b in self.app.button))
        self.assertNotIn("order_download", self.app.session_state)

    def test_validation_error_keeps_quantity_and_reason_for_correction(self):
        self.edit("109", "Не терять введённую причину")
        self.assertIn("422", self.errors())
        self.assertEqual(self.app.text_input(key=f"qty:{IDENTITY}").value, "109")
        self.assertEqual(self.app.text_area(key=f"reason:{IDENTITY}").value, "Не терять введённую причину")
        self.assertEqual(self.proposal()["version"], 1)
        self.edit("120", "Не терять введённую причину")
        self.assertEqual(self.proposal()["version"], 2)

    def test_conflicting_patch_preserves_draft_and_requires_explicit_rebase(self):
        client = self.client()
        current = client.get_proposal(PROPOSAL)
        client.edit_proposal(PROPOSAL, {
            "expected_version": current["version"], "edits": [{"line_id": "line-tools", "purchase_qty": "132"}],
            "reason": "Правка другого пользователя",
        })
        self.edit("120", "Мой сохранённый черновик")
        self.assertEqual(self.proposal()["version"], 2)
        self.assertEqual(self.proposal()["lines"][0]["selected_purchase_qty"], "132")
        self.assertEqual(self.app.text_input(key=f"qty:{IDENTITY}").value, "120")
        self.assertEqual(self.app.text_area(key=f"reason:{IDENTITY}").value, "Мой сохранённый черновик")
        self.assertTrue(self.button("Сохранить количество").disabled)
        self.assertTrue(any("Сохранённый ввод не применён" in str(item.value) for item in self.app.warning))

    def test_missing_role_is_not_shown_as_success_and_draft_survives(self):
        def forbidden(*_args, **_kwargs):
            raise ApiError(403, "MISSING_ROLE", "Нет роли approver", retryable=False)

        self.client().approve_proposal = forbidden
        self.app.text_input(key=f"qty:{IDENTITY}").set_value("108")
        self.app.text_area(key=f"reason:{IDENTITY}").set_value("Сохранить черновик при ошибке")
        self.app.button(key=f"approve:{PROPOSAL}").click().run()
        self.assert_no_crash()
        self.assertIn("403", self.errors())
        self.assertEqual(self.proposal()["status"], "draft")
        self.assertFalse(any(b.label == "Подготовить CSV" for b in self.app.button))
        self.assertNotIn("order_download", self.app.session_state)
        self.assertEqual(self.app.text_input(key=f"qty:{IDENTITY}").value, "108")
        self.assertEqual(self.app.text_area(key=f"reason:{IDENTITY}").value, "Сохранить черновик при ошибке")

    def test_approval_of_stale_review_is_blocked_before_post(self):
        client = self.client()
        current = client.get_proposal(PROPOSAL)
        client.edit_proposal(PROPOSAL, {
            "expected_version": current["version"], "edits": [{"line_id": "line-tools", "purchase_qty": "120"}],
            "reason": "Правка другого пользователя",
        })
        self.app.button(key=f"approve:{PROPOSAL}").click().run()
        self.assert_no_crash()
        self.assertIn("409", self.errors())
        self.assertEqual(self.proposal()["status"], "draft")
        self.assertEqual(self.proposal()["version"], 2)

    def test_export_timeout_retains_key_for_same_logical_attempt(self):
        client = self.client()
        original = client.export_proposal
        original_approve = client.approve_proposal
        approvals = []
        calls = []

        def approve_once(*args):
            approvals.append(args)
            return original_approve(*args)

        def timeout_once(proposal_id, payload):
            calls.append(dict(payload))
            if len(calls) == 1:
                raise ApiError(None, "TIMEOUT", "Ответ неизвестен", retryable=True, ambiguous=True)
            return original(proposal_id, payload)

        client.export_proposal = timeout_once
        client.approve_proposal = approve_once
        self.app.button(key=f"approve:{PROPOSAL}").click().run()
        self.assert_no_crash()
        self.assertNotIn("order_download", self.app.session_state)
        self.assertIn("TIMEOUT", self.errors())
        self.assertEqual(self.proposal()["status"], "approved")
        self.assertFalse(any(b.key == f"approve:{PROPOSAL}" for b in self.app.button))
        self.app.button(key=f"export:{PROPOSAL}").click().run()
        self.assert_no_crash()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(len(approvals), 1)
        self.assertIn("order_download", self.app.session_state)

    def test_version_change_between_approval_and_export_never_downloads_new_draft(self):
        client = self.client()
        original = client.approve_proposal

        def approve_then_concurrent_edit(proposal_id, payload):
            result = original(proposal_id, payload)
            client.edit_proposal(proposal_id, {
                "expected_version": payload["expected_version"],
                "edits": [{"line_id": "line-tools", "purchase_qty": "120"}],
                "reason": "Другой закупщик изменил заказ после утверждения",
            })
            return result

        client.approve_proposal = approve_then_concurrent_edit
        self.app.button(key=f"approve:{PROPOSAL}").click().run()
        self.assert_no_crash()
        self.assertEqual(self.proposal()["status"], "draft")
        self.assertEqual(self.proposal()["version"], 2)
        self.assertNotIn("order_download", self.app.session_state)
        self.assertIn("409", self.errors())

    def test_navigation_retains_line_draft_and_does_not_submit_it(self):
        self.app.text_input(key=f"qty:{IDENTITY}").set_value("120").run()
        self.app.text_area(key=f"reason:{IDENTITY}").set_value("Вернуться к этой правке").run()
        self.app.radio(key="workspace_page").set_value("Данные").run()
        self.assert_no_crash()
        self.app.radio(key="workspace_page").set_value("Заказы").run()
        self.assert_no_crash()
        self.assertEqual(self.proposal()["version"], 1)
        self.assertEqual(self.app.text_input(key=f"qty:{IDENTITY}").value, "120")
        self.assertEqual(self.app.text_area(key=f"reason:{IDENTITY}").value, "Вернуться к этой правке")

    def test_unsaved_quantity_cannot_be_ignored_by_approval(self):
        self.app.text_input(key=f"qty:{IDENTITY}").set_value("120").run()
        self.assert_no_crash()
        self.assertFalse(any(b.key == f"approve:{PROPOSAL}" for b in self.app.button))
        self.assertEqual(self.proposal()["version"], 1)
        self.app.button(key=f"discard:{IDENTITY}").click().run()
        self.assert_no_crash()
        self.assertEqual(self.app.text_input(key=f"qty:{IDENTITY}").value, "108")
        self.assertFalse(self.app.button(key=f"approve:{PROPOSAL}").disabled)

    def test_orders_do_not_request_unrelated_sources_or_projects(self):
        def unexpected(*args, **kwargs):
            raise AssertionError("Inactive pages must not make API requests")

        self.client().list_sources = unexpected
        self.client().list_demand_events = unexpected
        self.app.run()
        self.assert_no_crash()
        self.assertFalse(any(b.label == "Рассчитать заказ" for b in self.app.button))


@unittest.skipUnless(AppTest is not None, "Streamlit is needed to import UI configuration")
class CredentialScopeTests(unittest.TestCase):
    def test_configured_token_never_follows_an_edited_endpoint(self):
        from apps.buyer_ui.app import _http_client

        with patch.dict(os.environ, {"BUYER_API_URL": "https://api.example.test", "BUYER_API_TOKEN": "synthetic-test-token"}):
            with patch("apps.buyer_ui.app.HttpClient") as transport:
                with self.assertRaises(ValueError):
                    _http_client("https://different.example.test")
                transport.assert_not_called()

    def test_configured_endpoint_retains_server_credentials(self):
        from apps.buyer_ui.app import _http_client

        with patch.dict(os.environ, {"BUYER_API_URL": "https://api.example.test", "BUYER_API_TOKEN": "synthetic-test-token"}):
            with patch("apps.buyer_ui.app.HttpClient") as transport:
                _http_client("https://api.example.test/")
                transport.assert_called_once_with("https://api.example.test/", timeout=10, token="synthetic-test-token")


if __name__ == "__main__":
    unittest.main()
