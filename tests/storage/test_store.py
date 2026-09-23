from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Barrier, Event

import pytest

from ekt.storage import IdempotencyConflictError, Store, VersionConflictError


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "state.db") as result:
        yield result


def test_content_versions_and_approval_audit_survive_restart(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    original = {"proposal_id": "p1", "status": "draft", "quantity": "108", "content_hash": "h1"}
    draft = store.create_item("proposals", "p1", original)
    approved = store.mutate_proposal(
        "p1", 1, lambda value: {**value, "status": "approved"},
        audit_event={"event_type": "approved", "actor": "buyer", "content_hash": "h1"},
        bump_version=False,
    )
    assert approved["version"] == draft["version"] == 1
    assert store.get_item("proposals", "p1", version=1) == draft
    # Editing approved content invalidates approval in the domain mutation.
    edited = store.mutate_proposal(
        "p1", 1, lambda value: {**value, "status": "draft", "quantity": "120", "content_hash": "h2"},
        audit_event={"event_type": "edited", "actor": "buyer", "reason": "project confirmed"},
    )
    store.close()
    reopened = Store(path)
    assert reopened.get_item("proposals", "p1") == edited
    assert edited["version"] == 2
    assert reopened.get_item("proposals", "p1", version=1) == draft
    audit = reopened.list_audit("proposals", "p1")
    assert [(entry["event_type"], entry["payload"]["item_version"]) for entry in audit] == [
        ("approved", 1), ("edited", 2)
    ]
    assert "version" not in original  # Caller-owned objects are not mutated.


def test_concurrent_edits_cannot_overwrite_each_other(store):
    store.create_item("proposals", "p1", {"status": "draft", "quantity": "108"})
    barrier = Barrier(2)

    def edit(quantity):
        barrier.wait()
        try:
            return store.mutate_proposal(
                "p1", 1, lambda value: {**value, "quantity": quantity},
                audit_event={"event_type": "edited", "quantity": quantity},
            )
        except VersionConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["120", "132"]))
    assert sum(isinstance(value, VersionConflictError) for value in results) == 1
    assert store.get_item("proposals", "p1")["version"] == 2
    assert len(store.list_audit("proposals", "p1")) == 1


def test_edit_racing_approval_never_approves_new_content(store):
    store.create_item("proposals", "p1", {
        "status": "draft", "quantity": "108", "content_hash": "h1",
    })
    barrier = Barrier(2)

    def approve():
        barrier.wait()

        def validate_and_approve(value):
            assert value["content_hash"] == "h1"
            return {**value, "status": "approved"}

        try:
            store.mutate_proposal("p1", 1, validate_and_approve, bump_version=False)
        except VersionConflictError:
            pass  # Valid outcome: the edit committed first.

    def edit():
        barrier.wait()
        store.mutate_proposal(
            "p1", 1,
            lambda value: {**value, "status": "draft", "quantity": "120", "content_hash": "h2"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(approve), pool.submit(edit)]
        for future in futures:
            future.result()
    current = store.get_item("proposals", "p1")
    assert (current["version"], current["quantity"], current["status"]) == (2, "120", "draft")


def test_export_retry_atomically_reuses_saved_bytes_and_audit(store):
    store.create_item("proposals", "p1", {"status": "approved", "content_hash": "h1"})
    barrier = Barrier(2)

    def export(candidate_id):
        barrier.wait()
        with store.transaction() as tx:
            proposal = tx.get_item("proposals", "p1")
            assert proposal["version"] == 1 and proposal["status"] == "approved"
            reserved = tx.reserve_idempotency("export:p1", "key-1", "p1:v1:h1", candidate_id)
            if not reserved["created"]:
                return tx.get_item("exports", reserved["result_id"])
            saved = tx.create_item("exports", candidate_id, {
                "csv": "ДЕМОНСТРАЦИЯ — НЕ ЗАКАЗ ПОСТАВЩИКУ\nsku,qty\n001,108\n",
                "proposal_id": "p1", "proposal_version": 1,
            })
            tx.append_audit("proposals", "p1", "exported", {"export_id": candidate_id})
            return saved

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(export, ["export-a", "export-b"]))
    assert results[0] == results[1]
    assert len(store.list_items("exports")) == 1
    assert len(store.list_audit("proposals", "p1")) == 1


def test_idempotency_rejects_payload_change_but_is_scoped(store):
    assert store.reserve_idempotency("runs", "key", "hash-a", "run1")["created"] is True
    assert store.reserve_idempotency("runs", "key", "hash-a", "run2") == {
        "created": False, "result_id": "run1",
    }
    with pytest.raises(IdempotencyConflictError):
        store.reserve_idempotency("runs", "key", "hash-b", "run3")
    assert store.get_idempotency("runs", "key")["result_id"] == "run1"
    assert store.reserve_idempotency("scenarios", "key", "hash-b", "scenario1")["created"] is True


def test_failed_transaction_leaves_no_idempotency_artifact_or_audit(store):
    with pytest.raises(RuntimeError, match="render failed"):
        with store.transaction() as tx:
            tx.reserve_idempotency("exports", "key", "hash", "export1")
            tx.create_item("exports", "export1", {"csv": "incomplete"})
            tx.append_audit("proposals", "p1", "exported", {"export_id": "export1"})
            raise RuntimeError("render failed")
    assert store.get_idempotency("exports", "key") is None
    assert store.get_item("exports", "export1") is None
    assert store.list_audit("proposals", "p1") == []


def test_invalid_mutation_rolls_back_without_consuming_version(store):
    original = store.create_item("proposals", "p1", {"status": "draft", "quantity": "108"})

    def invalid_edit(value):
        value["quantity"] = "999"
        raise ValueError("invalid pack multiple")

    with pytest.raises(ValueError, match="invalid pack"):
        store.mutate_proposal("p1", 1, invalid_edit)
    assert store.get_item("proposals", "p1") == original
    assert store.get_item("proposals", "p1", version=2) is None


def test_restart_marks_only_unfinished_work_retryable(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    for kind, item_id, status in [
        ("jobs", "j1", "queued"), ("runs", "r1", "running"),
        ("scenarios", "s1", "running"), ("runs", "r2", "succeeded"),
        ("jobs", "j2", "failed"),
    ]:
        store.create_item(kind, item_id, {"id": item_id, "status": status})
    store.close()
    restarted = Store(path)
    assert restarted.recover_running_jobs() == 3
    assert restarted.recover_running_jobs() == 0
    for kind, item_id in [("jobs", "j1"), ("runs", "r1"), ("scenarios", "s1")]:
        item = restarted.get_item(kind, item_id)
        assert item["status"] == "failed"
        assert item["error"]["retryable"] is True
        assert item["error"]["code"] == "WORKER_RESTARTED"
    assert restarted.get_item("runs", "r2")["status"] == "succeeded"
    assert restarted.get_item("jobs", "j2")["version"] == 1


def test_nonfinite_json_cannot_enter_persisted_state(store):
    with pytest.raises(ValueError):
        store.create_item("forecasts", "f1", {"mean": float("nan")})
    assert store.get_item("forecasts", "f1") is None


def test_in_memory_store_keeps_connections_alive():
    with Store(":memory:") as store:
        store.create_item("snapshots", "s1", {"mode": "synthetic_demo"})
        assert store.get_item("snapshots", "s1")["mode"] == "synthetic_demo"


@pytest.mark.parametrize("operation", ["read", "edit"])
def test_in_memory_concurrency_waits_for_transaction_instead_of_sqlite_lock_error(operation):
    with Store(":memory:") as store:
        store.create_item("proposals", "p1", {"quantity": "108"})
        writer_ready, contender_started, release_writer = Event(), Event(), Event()

        def hold_write():
            with store.transaction() as tx:
                saved = tx.mutate_proposal("p1", 1, lambda value: {**value, "quantity": "120"})
                writer_ready.set()
                assert release_writer.wait(timeout=2)
                return saved

        def contend():
            contender_started.set()
            if operation == "read":
                return store.get_item("proposals", "p1")
            return store.mutate_proposal("p1", 1, lambda value: {**value, "quantity": "132"})

        with ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(hold_write)
            try:
                assert writer_ready.wait(timeout=2)
                contender = pool.submit(contend)
                assert contender_started.wait(timeout=2)
                with pytest.raises(TimeoutError):
                    contender.result(timeout=0.05)
            finally:
                release_writer.set()
            saved = writer.result(timeout=2)
            if operation == "read":
                assert contender.result(timeout=2) == saved
            else:
                with pytest.raises(VersionConflictError):
                    contender.result(timeout=2)
            assert store.get_item("proposals", "p1")["quantity"] == "120"
