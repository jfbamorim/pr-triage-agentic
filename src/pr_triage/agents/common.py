import os

from pydantic import BaseModel


class AgentFailure(BaseModel):
    code: str
    stage: str
    message: str
    retryable: bool


def default_model() -> str:
    return "openai:" + os.getenv("OPENAI_AGENT_MODEL", "gpt-4.1-mini")
