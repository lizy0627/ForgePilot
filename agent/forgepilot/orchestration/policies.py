"""Failure classification policy; Repair is a declared future action, not executed here."""
from __future__ import annotations

from hashlib import sha256
import json
from typing import Protocol

from .models import FailureKind, PolicyAction


class FailurePolicy(Protocol):
    @property
    def definition_hash(self) -> str: ...
    def action_for(self, failure: FailureKind, *, attempt: int) -> PolicyAction: ...


class DefaultFailurePolicy:
    """The section-10 policy expressed as data and kept separate from execution."""

    _ACTIONS = {
        FailureKind.MODEL_TRANSIENT: PolicyAction.RETRY,
        FailureKind.MODEL_RATE_LIMIT: PolicyAction.RETRY,
        FailureKind.MODEL_CONTRACT_VIOLATION: PolicyAction.REPAIR,
        FailureKind.TOOL_TRANSIENT: PolicyAction.RETRY,
        FailureKind.TOOL_POLICY_VIOLATION: PolicyAction.STOP,
        FailureKind.REPOSITORY_CONFLICT: PolicyAction.PAUSE,
        FailureKind.WORKSPACE_CORRUPTED: PolicyAction.STOP,
        FailureKind.BUILD_CONFIGURATION: PolicyAction.PAUSE,
        FailureKind.BUILD_CODE_ERROR: PolicyAction.REPAIR,
        FailureKind.TEST_FAILURE: PolicyAction.REPAIR,
        FailureKind.TEST_INTEGRITY_VIOLATION: PolicyAction.STOP,
        FailureKind.HIDDEN_ORACLE_FAILURE: PolicyAction.REPAIR,
        FailureKind.STORAGE_FAILURE: PolicyAction.STOP,
        FailureKind.USER_CANCELLED: PolicyAction.STOP,
        FailureKind.APPLICATION_INTERRUPTED: PolicyAction.PAUSE,
    }

    def __init__(self, *, max_retries: int = 3) -> None:
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        self.max_retries = max_retries

    @property
    def definition_hash(self) -> str:
        payload = {
            "name": "default-failure-policy.v1", "max_retries": self.max_retries,
            "actions": {key.value: value.value for key, value in self._ACTIONS.items()},
        }
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def action_for(self, failure: FailureKind, *, attempt: int) -> PolicyAction:
        action = self._ACTIONS[FailureKind(failure)]
        if action is PolicyAction.RETRY and attempt > self.max_retries:
            return PolicyAction.STOP
        return action


__all__ = ["DefaultFailurePolicy", "FailurePolicy"]
