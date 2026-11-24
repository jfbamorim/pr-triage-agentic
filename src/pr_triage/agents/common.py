import os

from dotenv import load_dotenv
from pydantic import BaseModel
from pydantic_ai.exceptions import (
    ModelAPIError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
    UserError,
)

load_dotenv()


class AgentFailure(BaseModel):
    code: str
    stage: str
    message: str
    retryable: bool


def default_model() -> str:
    return "openai:" + os.getenv("OPENAI_AGENT_MODEL", "gpt-4.1-mini")


def classify(error: Exception) -> tuple[str, bool]:
    if isinstance(error, UserError):
        return "configuration_error", False
    if isinstance(error, UsageLimitExceeded):
        return "step_limit", False
    if isinstance(error, UnexpectedModelBehavior):
        return "invalid_output", True
    if isinstance(error, ModelAPIError):
        return "llm_unavailable", True
    return "unexpected_error", False
