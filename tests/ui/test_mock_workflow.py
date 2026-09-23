"""Mock UI workflow checks; these do not validate server calculations or storage."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import unittest

from apps.buyer_ui.client import ApiError, MockClient


class MockWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.client = MockClient()
        self.proposal_id = "demo-proposal-tools"

    def proposal(self):
        return self.client.get_proposal(self.proposal_id)

    def approve_current(self):
        current = self.proposal()
        self.client.approve_proposal(self.proposal_id, {
            "expected_version": current["version"], "content_hash": current["content_hash"]
        })
        return self.proposal()

    def change_quantity(self, quantity="120", reason="Подтверждённая потребность демонстрации"):
        current = self.proposal()
        return self.client.edit_proposal(self.proposal_id, {
            "expected_version": current["version"],
            "edits": [{"line_id": "line-tools", "purchase_qty": quantity}],
            "reason": reason,
        })

    def test_fixture_has_two_suppliers_and_keeps_incompatible_uoms_separate(self):
        summaries = self.client.list_proposals(run_id="demo-run")["items"]
        details = [self.client.get_proposal(item["proposal_id"]) for item in summaries]
        self.assertEqual(len({item["supplier_id"] for item in details}), 2)
        self.assertEqual(len({item["warehouse_id"] for item in details}), 1)
        self.assertEqual({item["mode"] for item in details}, {"synthetic_demo"})
        self.assertGreater(len({line["purchase_uom"] for item in details for line in item["lines"]}), 1)

    def test_golden_and_all_fixture_ledgers_equal_selected_base_quantity(self):
        summaries = self.client.list_proposals(run_id="demo-run")["items"]
        for summary in summaries:
            proposal = self.client.get_proposal(summary["proposal_id"])
            for line in proposal["lines"]:
                with self.subTest(line_id=line["line_id"]):
                    ledger = sum((Decimal(part["delta_base_qty"]) for part in line["explanation"]), Decimal("0"))
                    self.assertEqual(ledger, Decimal(line["selected_base_qty"]))
        tools = next(line for line in self.proposal()["lines"] if line["line_id"] == "line-tools")
        self.assertEqual(Decimal(tools["selected_base_qty"]), Decimal("108"))
        self.assertEqual(Decimal(tools["moq_purchase"]), Decimal("108"))
        self.assertEqual(Decimal(tools["pack_multiple_purchase"]), Decimal("12"))

    def test_export_of_draft_is_rejected(self):
        current = self.proposal()
        self.assertEqual(current["status"], "draft")
        self.assertFalse(current["capabilities"]["can_export"])
        with self.assertRaises(ApiError):
            self.client.export_proposal(self.proposal_id, {
                "expected_version": current["version"], "idempotency_key": "draft-export"
            })

    def test_approval_edit_reapproval_and_export_are_bound_to_current_version(self):
        approved = self.approve_current()
        self.assertEqual(approved["status"], "approved")
        self.assertTrue(approved["capabilities"]["can_export"])
        first_export = self.client.export_proposal(self.proposal_id, {
            "expected_version": approved["version"], "idempotency_key": "approved-v1"
        })
        self.assertIn("ДЕМОНСТРАЦИЯ", first_export.data.decode("utf-8-sig"))
        self.assertIn("НЕ ЗАКАЗ ПОСТАВЩИКУ", first_export.data.decode("utf-8-sig"))

        edited = self.change_quantity()
        self.assertEqual(edited["version"], approved["version"] + 1)
        self.assertEqual(edited["status"], "draft")
        self.assertNotEqual(edited["content_hash"], approved["content_hash"])
        self.assertFalse(edited["capabilities"]["can_export"])
        for version, key in ((approved["version"], "approved-v1"), (edited["version"], "draft-v2")):
            with self.subTest(export_version=version), self.assertRaises(ApiError):
                self.client.export_proposal(self.proposal_id, {"expected_version": version, "idempotency_key": key})

        current = self.approve_current()
        second_export = self.client.export_proposal(self.proposal_id, {
            "expected_version": current["version"], "idempotency_key": "approved-v2"
        })
        self.assertNotEqual(first_export.data, second_export.data)
        self.assertIn("120", second_export.data.decode("utf-8-sig"))

    def test_manual_override_keeps_recommendation_and_explains_delta(self):
        before = deepcopy(self.proposal()["lines"])
        edited = self.change_quantity()
        line = next(line for line in edited["lines"] if line["line_id"] == "line-tools")
        original = next(line for line in before if line["line_id"] == "line-tools")
        self.assertEqual(line["recommended_base_qty"], original["recommended_base_qty"])
        self.assertEqual(line["recommended_purchase_qty"], original["recommended_purchase_qty"])
        manual = [part for part in line["explanation"] if part["code"] == "manual_override_delta"]
        self.assertEqual(len(manual), 1)
        self.assertEqual(Decimal(manual[0]["delta_base_qty"]), Decimal("12"))
        self.assertEqual(sum((Decimal(part["delta_base_qty"]) for part in line["explanation"]), Decimal("0")), Decimal(line["selected_base_qty"]))

    def test_stale_patch_and_hash_cannot_overwrite_new_version(self):
        old = self.proposal()
        self.change_quantity()
        after = self.proposal()
        with self.assertRaises(ApiError) as stale:
            self.client.edit_proposal(self.proposal_id, {
                "expected_version": old["version"],
                "edits": [{"line_id": "line-tools", "purchase_qty": "132"}], "reason": "stale"
            })
        self.assertEqual(stale.exception.status_code, 409)
        for version, content_hash in ((old["version"], old["content_hash"]), (after["version"], old["content_hash"])):
            with self.subTest(version=version), self.assertRaises(ApiError) as caught:
                self.client.approve_proposal(self.proposal_id, {"expected_version": version, "content_hash": content_hash})
            self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.proposal(), after)

    def test_invalid_quantity_and_empty_reason_leave_proposal_unchanged(self):
        before = self.proposal()
        for quantity, reason in (("109", "quantity unsupported"), ("-1", "negative"), ("NaN", "invalid"), ("120", " ")):
            with self.subTest(quantity=quantity, reason=reason), self.assertRaises(ApiError) as caught:
                self.change_quantity(quantity, reason)
            self.assertEqual(caught.exception.status_code, 422)
            self.assertEqual(self.proposal(), before)

    def test_unknown_line_cannot_edit_a_different_row_by_position(self):
        before = self.proposal()
        with self.assertRaises(ApiError):
            self.client.edit_proposal(self.proposal_id, {
                "expected_version": before["version"],
                "edits": [{"line_id": "line-cable", "purchase_qty": "120"}], "reason": "wrong proposal line"
            })
        self.assertEqual(self.proposal(), before)

    def test_zero_order_is_supported_without_moq_top_up(self):
        edited = self.change_quantity("0")
        line = next(line for line in edited["lines"] if line["line_id"] == "line-tools")
        self.assertEqual(Decimal(line["selected_purchase_qty"]), Decimal("0"))
        self.assertEqual(Decimal(line["selected_base_qty"]), Decimal("0"))
        self.assertEqual(sum((Decimal(part["delta_base_qty"]) for part in line["explanation"]), Decimal("0")), Decimal("0"))

    def test_export_repeat_returns_same_saved_bytes(self):
        approved = self.approve_current()
        payload = {"expected_version": approved["version"], "idempotency_key": "same-logical-export"}
        first = self.client.export_proposal(self.proposal_id, payload)
        second = self.client.export_proposal(self.proposal_id, payload)
        self.assertEqual(first.data, second.data)
        self.assertEqual(first.filename, second.filename)

    def test_scenario_is_isolated_and_repeated_key_does_not_create_new_job(self):
        before = deepcopy(self.proposal())
        payload = {"base_run_id": "demo-run", "overrides": {"service_target": 0.99, "lead_time_delay_days": 0}, "seed": 42, "idempotency_key": "same-scenario"}
        first = self.client.create_scenario(payload)
        second = self.client.create_scenario(payload)
        self.assertEqual(first["scenario_id"], second["scenario_id"])
        for _ in range(5):
            result = self.client.get_scenario(first["scenario_id"])
            if result["status"] in {"succeeded", "failed"}:
                break
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["base_run_id"], "demo-run")
        self.assertEqual(self.proposal(), before)

    def test_unsupported_budget_is_explicit_and_leaves_baseline_unchanged(self):
        before = self.proposal()
        with self.assertRaises(ApiError) as caught:
            self.client.create_scenario({
                "base_run_id": "demo-run", "overrides": {"budget_cap": "100.00"},
                "seed": 42, "idempotency_key": "budget"
            })
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(self.proposal(), before)

    def test_blocked_profile_disables_and_enforces_approval(self):
        client = MockClient(quality="blocked")
        proposal = client.get_proposal(self.proposal_id)
        self.assertFalse(proposal["capabilities"]["can_approve"])
        self.assertTrue(proposal["capabilities"]["reasons"])
        with self.assertRaises(ApiError):
            client.approve_proposal(self.proposal_id, {"expected_version": proposal["version"], "content_hash": proposal["content_hash"]})

    def test_reads_are_copies_so_local_widget_edits_do_not_mutate_mock(self):
        proposal = self.proposal()
        proposal["status"] = "approved"
        proposal["lines"][0]["selected_purchase_qty"] = "999999"
        current = self.proposal()
        self.assertEqual(current["status"], "draft")
        self.assertNotEqual(current["lines"][0]["selected_purchase_qty"], "999999")


if __name__ == "__main__":
    unittest.main()
