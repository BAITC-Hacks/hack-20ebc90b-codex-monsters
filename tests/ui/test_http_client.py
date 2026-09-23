"""Transport contract checks against a real loopback HTTP server, not a backend."""

from __future__ import annotations

import json
import threading
import time
import unittest
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from apps.buyer_ui.client import ApiError, HttpClient


class RecordingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), RecordingHandler)
        self.responses = deque()
        self.requests = []
        self.lock = threading.Lock()


class RecordingHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        with self.server.lock:
            self.server.requests.append(
                {"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body}
            )
            response = self.server.responses.popleft() if self.server.responses else {}
        time.sleep(response.get("delay", 0))
        payload = response.get("body", b'{}')
        if isinstance(payload, dict):
            payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(response.get("status", 200))
        self.send_header("Content-Type", response.get("content_type", "application/json"))
        self.send_header("Content-Length", str(len(payload)))
        for key, value in response.get("headers", {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # The timeout tests deliberately abandon the response.
            pass

    do_GET = _handle
    do_POST = _handle
    do_PATCH = _handle


class HttpClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = RecordingServer()
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=2)

    def setUp(self):
        with self.server.lock:
            self.server.requests.clear()
            self.server.responses.clear()
        self.client = HttpClient(self.url, timeout=1)

    def respond(self, **response):
        with self.server.lock:
            self.server.responses.append(response)

    def last_request(self):
        with self.server.lock:
            return self.server.requests[-1]

    def test_base_url_gets_exactly_one_v1_prefix(self):
        for base in (self.url, self.url + "/", self.url + "/v1", self.url + "/v1/"):
            with self.subTest(base=base):
                self.respond(body={"status": "ok", "version": "stub"})
                self.assertEqual(HttpClient(base).health()["status"], "ok")
                self.assertEqual(self.last_request()["path"], "/v1/health")

    def test_id_is_one_url_path_segment_and_version_is_query(self):
        self.respond(body={"proposal_id": "vendor/a?draft#1"})
        self.client.get_proposal("vendor/a?draft#1", version=7)
        request = self.last_request()
        self.assertEqual(request["method"], "GET")
        url = urlsplit(request["path"])
        self.assertEqual(url.path, "/v1/proposals/vendor%2Fa%3Fdraft%231")
        self.assertEqual(parse_qs(url.query), {"version": ["7"]})

    def test_list_filters_are_encoded_and_none_is_omitted(self):
        self.respond(body={"items": [], "next_cursor": None})
        result = self.client.list_proposals(
            run_id="run / один", supplier_id="ACME & sons", cursor="a+b=", limit=17
        )
        query = parse_qs(urlsplit(self.last_request()["path"]).query)
        self.assertEqual(query, {
            "run_id": ["run / один"], "supplier_id": ["ACME & sons"], "cursor": ["a+b="], "limit": ["17"]
        })
        self.assertEqual(result, {"items": [], "next_cursor": None})
        self.respond()
        self.client.list_proposals()
        self.assertNotIn("None", self.last_request()["path"])

    def test_patch_preserves_decimal_string_and_reason(self):
        payload = {"expected_version": 2, "edits": [{"line_id": "001", "purchase_qty": "120.000"}], "reason": "Уточнение закупки"}
        self.respond(body={"version": 3, "total_cost": None})
        result = self.client.edit_proposal("p1", payload)
        request = self.last_request()
        self.assertEqual(request["method"], "PATCH")
        self.assertEqual(request["path"], "/v1/proposals/p1")
        self.assertEqual(json.loads(request["body"]), payload)
        self.assertEqual(result, {"version": 3, "total_cost": None})

    def test_read_does_not_turn_decimal_strings_or_null_into_numbers(self):
        proposal = {"selected_purchase_qty": "0.10000000000000000001", "line_cost": None}
        self.respond(body=proposal)
        self.assertEqual(self.client.get_proposal("p1"), proposal)

    def test_mutating_routes_and_payloads_match_contract(self):
        cases = [
            ("/snapshots", self.client.create_snapshot, {"source_ids": ["s1"], "mapping_version": "m1", "mode": "synthetic_demo", "as_of": "2026-09-01T00:00:00+00:00"}),
            ("/planning-runs", self.client.create_planning_run, {"snapshot_id": "s1", "policy": {"service_metric": "cycle_service", "service_target": 0.95}, "idempotency_key": "run-key"}),
            ("/scenarios", self.client.create_scenario, {"base_run_id": "r1", "overrides": {"budget_cap": "1234.50"}, "seed": 42, "idempotency_key": "scenario-key"}),
            ("/snapshots/s1/buyer-inputs", lambda value: self.client.save_buyer_inputs("s1", value), {"items": [{"sku_id": "A", "free_base": "0.000000000001"}], "reason": "Сверено", "accept_history_estimate": True, "idempotency_key": "buyer-1"}),
            ("/proposals/p1/approve", lambda value: self.client.approve_proposal("p1", value), {"expected_version": 2, "content_hash": "exact-hash"}),
        ]
        for route, call, payload in cases:
            with self.subTest(route=route):
                self.respond(body={"id": "accepted"}, status=202)
                call(payload)
                request = self.last_request()
                self.assertEqual((request["method"], request["path"]), ("POST", "/v1" + route))
                self.assertEqual(json.loads(request["body"]), payload)

    def test_read_job_snapshot_run_scenario_and_event_routes(self):
        cases = [
            ("/sources", self.client.list_sources),
            ("/snapshots/s1/buyer-inputs", lambda: self.client.get_buyer_inputs("s1")),
            ("/jobs/j1", lambda: self.client.get_job("j1")),
            ("/snapshots/s1", lambda: self.client.get_snapshot("s1")),
            ("/planning-runs/r1", lambda: self.client.get_planning_run("r1")),
            ("/scenarios/c1", lambda: self.client.get_scenario("c1")),
        ]
        for route, call in cases:
            with self.subTest(route=route):
                self.respond(body={"id": "read"})
                self.assertEqual(call(), {"id": "read"})
                self.assertEqual((self.last_request()["method"], self.last_request()["path"]), ("GET", "/v1" + route))
        self.respond(body={"items": []})
        self.client.list_demand_events("run/1", label="suspected_project", cursor="a/b", limit=12)
        url = urlsplit(self.last_request()["path"])
        self.assertEqual(url.path, "/v1/planning-runs/run%2F1/demand-events")
        self.assertEqual(parse_qs(url.query), {"label": ["suspected_project"], "cursor": ["a/b"], "limit": ["12"]})

    def test_csv_export_preserves_exact_backend_bytes_and_retry_key(self):
        csv_bytes = '\ufeffДЕМОНСТРАЦИЯ — НЕ ЗАКАЗ ПОСТАВЩИКУ\r\nsku,qty\r\n0007,120.000\r\n'.encode("utf-8")
        payload = {"expected_version": 3, "idempotency_key": "logical-export-1"}
        for _ in range(2):
            self.respond(body=csv_bytes, content_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="approved-v3.csv"'})
            exported = self.client.export_proposal("p1", payload)
            self.assertEqual(exported.data, csv_bytes)
            self.assertEqual(exported.filename, "approved-v3.csv")
            self.assertEqual(self.last_request()["path"], "/v1/proposals/p1/export")
            self.assertEqual(json.loads(self.last_request()["body"]), payload)
        self.assertEqual(len(self.server.requests), 2)

    def test_server_errors_keep_status_code_details_and_retryability(self):
        for status, code in ((403, "MISSING_ROLE"), (409, "STALE_VERSION"), (422, "INVALID_QUANTITY")):
            with self.subTest(status=status):
                self.respond(status=status, body={"code": code, "message": "Изменение отклонено", "details": {"expected_version": 4}, "retryable": False})
                with self.assertRaises(ApiError) as caught:
                    self.client.approve_proposal("p1", {"expected_version": 3, "content_hash": "stale"})
                error = caught.exception
                self.assertEqual(error.status_code, status)
                self.assertEqual(error.code, code)
                self.assertEqual(error.details, {"expected_version": 4})
                self.assertFalse(error.retryable)

    def test_malformed_json_is_an_explicit_error(self):
        for invalid in (b"{this is not JSON", b"[]", b'{"value":NaN}', b'{"value":Infinity}'):
            with self.subTest(body=invalid):
                self.respond(body=invalid, content_type="application/json")
                with self.assertRaises(ApiError) as caught:
                    self.client.get_proposal("p1")
                self.assertEqual(caught.exception.code, "INVALID_RESPONSE")

    def test_json_success_cannot_be_downloaded_as_approved_csv(self):
        self.respond(body={"message": "not a CSV file"})
        with self.assertRaises(ApiError) as caught:
            self.client.export_proposal("p1", {"expected_version": 2, "idempotency_key": "csv-key"})
        self.assertEqual(caught.exception.code, "INVALID_RESPONSE")
        self.assertTrue(caught.exception.ambiguous)

    def test_redirect_is_not_followed_with_configured_token(self):
        self.respond(status=307, headers={"Location": self.url + "/other"})
        with self.assertRaises(ApiError) as caught:
            HttpClient(self.url, token="synthetic-token").approve_proposal("p1", {
                "expected_version": 1, "content_hash": "h1"
            })
        self.assertEqual(caught.exception.status_code, 307)
        self.assertEqual(len(self.server.requests), 1)

    def test_internal_mutation_error_is_ambiguous_without_automatic_retry(self):
        self.respond(status=500, body={"code": "INTERNAL_ERROR", "message": "Unknown result", "details": {}, "retryable": True})
        with self.assertRaises(ApiError) as caught:
            self.client.export_proposal("p1", {"expected_version": 2, "idempotency_key": "csv-key"})
        self.assertEqual(caught.exception.status_code, 500)
        self.assertTrue(caught.exception.ambiguous)
        self.assertEqual(len(self.server.requests), 1)

    def test_request_rejects_nonfinite_number_before_network(self):
        with self.assertRaises(ApiError) as caught:
            self.client.create_scenario({"overrides": {"service_target": float("nan")}})
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        self.assertEqual(len(self.server.requests), 0)

    def test_non_json_error_does_not_become_mock_success(self):
        self.respond(status=503, body=b"<html>upstream unavailable</html>", content_type="text/html")
        with self.assertRaises(ApiError) as caught:
            self.client.list_proposals()
        self.assertEqual(caught.exception.status_code, 503)

    def test_read_timeout_is_retryable_but_not_ambiguous_mutation(self):
        self.respond(body={"status": "ok"}, delay=0.15)
        with self.assertRaises(ApiError) as caught:
            HttpClient(self.url, timeout=0.03).health()
        self.assertEqual(caught.exception.code, "TIMEOUT")
        self.assertTrue(caught.exception.retryable)
        self.assertFalse(caught.exception.ambiguous)

    def test_mutation_timeout_requires_reconciliation_and_is_not_retried(self):
        self.respond(body={"version": 2}, delay=0.15)
        with self.assertRaises(ApiError) as caught:
            HttpClient(self.url, timeout=0.03).edit_proposal("p1", {"expected_version": 1, "edits": [], "reason": "test"})
        self.assertEqual(caught.exception.code, "TIMEOUT")
        self.assertTrue(caught.exception.retryable)
        self.assertTrue(caught.exception.ambiguous)
        self.assertEqual(len(self.server.requests), 1)

    def test_optional_bearer_token_is_sent_only_when_configured(self):
        self.respond()
        HttpClient(self.url, token="synthetic-test-token").health()
        self.assertEqual(self.last_request()["headers"].get("Authorization"), "Bearer synthetic-test-token")
        self.respond()
        HttpClient(self.url).health()
        self.assertNotIn("Authorization", self.last_request()["headers"])


if __name__ == "__main__":
    unittest.main()
