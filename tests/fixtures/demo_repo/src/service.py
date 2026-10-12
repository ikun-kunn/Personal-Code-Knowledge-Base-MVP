"""Fixture module: exercises decorators, classes, methods and a long function."""

import time
from dataclasses import dataclass


class ModelProviderError(Exception):
    """Raised when a provider cannot fulfil a request."""


@dataclass
class RetryPolicy:
    max_retries: int = 3
    base_delay: float = 0.5

    def next_delay(self, attempt: int) -> float:
        """Exponential backoff: each attempt waits twice as long as the last."""
        return self.base_delay * (2**attempt)


class RetryProvider:
    def __init__(self, policy: RetryPolicy | None = None) -> None:
        self.policy = policy or RetryPolicy()

    def send(self, payload: dict) -> dict:
        return self._dispatch(payload, attempt=0)

    def _dispatch(self, payload: dict, attempt: int) -> dict:
        if attempt >= self.policy.max_retries:
            raise ModelProviderError("retry budget exhausted")
        time.sleep(self.policy.next_delay(attempt))
        return payload


@staticmethod
def build_payload(name: str) -> dict:
    return {"name": name, "kind": "request"}


def unreachable_helper() -> None:
    value = 1
    for index in range(10):
        value += index
    return None
