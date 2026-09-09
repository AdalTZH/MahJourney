from __future__ import annotations

import json
from typing import Any

from openai import OpenAI, OpenAIError

from .config import Settings
from .domain import AgentResult, AgentTask


class OpenAIGateway:
    """Strict, non-authoritative language layer for dispatcher explanations."""

    _schema = {
        "type": "object",
        "properties": {
            "reply": {"type": "string"},
            "evidence_references": {"type": "array", "items": {"type": "string"}},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["reply", "evidence_references", "warnings"],
        "additionalProperties": False,
    }

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None

    def explain(self, task: AgentTask, result: AgentResult, fallback: str) -> str:
        if self.client is None:
            return fallback
        evidence = list(result.evidence_references)
        prompt = {
            "task_type": task.task_type,
            "status": result.status,
            "computed_metrics": result.computed_metrics,
            "proposed_actions": result.proposed_actions,
            "warnings": result.warnings,
            "evidence_references": evidence,
        }
        for attempt in range(2):
            try:
                response = self.client.responses.create(
                    model=self.settings.openai_model,
                    store=False,
                    reasoning={"effort": "low"},
                    instructions=(
                        "You are MahJourney's Master Dispatcher. Explain only supplied computed "
                        "evidence. Never claim an action was approved, activated, or sent. "
                        "Return JSON."
                    ),
                    input=json.dumps(prompt, separators=(",", ":")),
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "dispatcher_reply",
                            "strict": True,
                            "schema": self._schema,
                        }
                    },
                    max_output_tokens=500,
                )
            except OpenAIError:
                return fallback
            try:
                parsed: dict[str, Any] = json.loads(response.output_text)
                if parsed["evidence_references"] != evidence:
                    raise ValueError("model evidence differs from computed evidence")
                return str(parsed["reply"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                if attempt:
                    return fallback
        return fallback

    def embed(self, text: str) -> tuple[float, ...] | None:
        if self.client is None:
            return None
        try:
            response = self.client.embeddings.create(
                model=self.settings.openai_embedding_model,
                input=text,
                encoding_format="float",
            )
        except OpenAIError:
            return None
        return tuple(response.data[0].embedding)
