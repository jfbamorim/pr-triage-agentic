from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from pr_triage.erp import ErpConflict, ErpRejected, ErpResponseLost, ErpUnavailable


class ToolError(BaseModel):
    code: str
    message: str
    retryable: bool = False
    detail: str | None = None


class ToolResult(BaseModel):
    ok: bool
    data: Any = None
    error: ToolError | None = None


def to_json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [to_json(item) for item in value]
    return value


def success(data: Any = None) -> ToolResult:
    return ToolResult(ok=True, data=to_json(data))


def failure(
    code: str, message: str, retryable: bool = False, detail: str | None = None
) -> ToolResult:
    return ToolResult(
        ok=False,
        error=ToolError(code=code, message=message, retryable=retryable, detail=detail),
    )


def guarded(operation: Callable[[], Any]) -> ToolResult:
    try:
        outcome = operation()
    except ErpUnavailable as error:
        return failure("erp_unavailable", str(error), retryable=True)
    except ErpResponseLost as error:
        return failure(
            "response_lost",
            f"{error}. Repeat the same call: it is idempotent.",
            retryable=True,
        )
    except ErpConflict as error:
        return failure("conflict", str(error))
    except ErpRejected as error:
        return failure(error.code, error.reason)
    return outcome if isinstance(outcome, ToolResult) else success(outcome)
