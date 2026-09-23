"""Adapt dictionaries to A's runtime contracts without defining competing models.

While A's foundation is absent, functions return the documented wire dictionary.
Once ``ekt.contracts`` exports the named Pydantic model, every result is validated
and returned as that model. Validation errors are deliberately not swallowed.
"""
from __future__ import annotations

import importlib
from collections.abc import Mapping
from typing import Any


def as_payload(value: Any) -> dict:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python")
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("Expected a shared contract model or mapping")


def _contracts():
    try:
        return importlib.import_module("ekt.contracts")
    except ModuleNotFoundError as exc:
        if exc.name != "ekt.contracts":
            raise
        return None


def export_contract(name: str, payload: dict):
    contracts = _contracts()
    if contracts is None:
        return payload
    model = getattr(contracts, name, None)
    if model is None:
        raise ImportError(f"ekt.contracts must export {name}; see role B handoff")
    return model.model_validate(payload)


def raise_domain(code: str, message: str, affected_sku_ids=(), retryable=False):
    contracts = _contracts()
    error_type = getattr(contracts, "DomainError", None) if contracts else None
    if error_type:
        raise error_type(code=code, message=message,
                         affected_sku_ids=list(affected_sku_ids), retryable=retryable)
    # An ordinary built-in exception keeps B usable before the foundation lands;
    # these attributes match the documented DomainError boundary for the worker.
    error = ValueError(message)
    error.code = code
    error.message = message
    error.affected_sku_ids = list(affected_sku_ids)
    error.retryable = retryable
    raise error
