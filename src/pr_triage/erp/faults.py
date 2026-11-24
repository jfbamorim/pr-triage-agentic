from pydantic import BaseModel


class ErpUnavailable(Exception):
    retryable = True


class ErpResponseLost(Exception):
    retryable = True


class ErpConflict(Exception):
    retryable = False


class ErpRejected(Exception):
    retryable = False

    def __init__(self, code: str, reason: str):
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


class Faults(BaseModel):
    unavailable: set[str] = set()
    fail_calls: dict[str, list[int]] = {}
    drop_response: dict[str, list[int]] = {}
