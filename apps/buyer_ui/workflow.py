"""Review orchestration only: constraints and approval decisions belong to API."""
from .client import ApiError


def refresh_review(client, reviewed, capability=None):
    """Never silently approve a version different from the one the buyer reviewed."""
    latest = client.get_proposal(reviewed["proposal_id"])
    identity = ("proposal_id", "version", "content_hash", "run_id", "snapshot_id")
    if any(latest.get(key) != reviewed.get(key) for key in identity):
        raise ApiError(status_code=409, code="STALE_REVIEW",
                       message="Предложение изменилось после просмотра. Проверьте актуальную версию.",
                       details={"current_version": latest.get("version")}, retryable=False)
    if capability and not latest.get("capabilities", {}).get(capability, False):
        raise ApiError(status_code=403, code="ACTION_UNAVAILABLE",
                       message="Сервер не разрешает это действие для текущего предложения.",
                       details={"reasons": latest.get("capabilities", {}).get("reasons", [])}, retryable=False)
    return latest


def approve_reviewed(client, reviewed, *, acknowledge_assumptions=False):
    latest = refresh_review(client, reviewed, "can_approve")
    payload = {"expected_version": latest["version"], "content_hash": latest["content_hash"]}
    if acknowledge_assumptions:
        payload["acknowledge_assumptions"] = True
    return client.approve_proposal(latest["proposal_id"], payload)


def export_reviewed(client, reviewed, request_key):
    latest = refresh_review(client, reviewed, "can_export")
    return client.export_proposal(latest["proposal_id"], {
        "expected_version": latest["version"], "idempotency_key": request_key,
    })
