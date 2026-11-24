from openai import OpenAI

from pr_triage.domain.schemas import Evidence
from pr_triage.rag.chunking import load_chunks
from pr_triage.rag.retrieve import fetch_by_id, retrieve


class PolicyIndex:
    def __init__(self, client: OpenAI):
        self.client = client

    def search(self, query: str, top_k: int) -> list[Evidence]:
        return retrieve(query, self.client, top_k=top_k)

    def clause_ids(self) -> set[str]:
        return {chunk.chunk_id for chunk in load_chunks()}

    def get(self, chunk_id: str) -> Evidence:
        return fetch_by_id(chunk_id)
