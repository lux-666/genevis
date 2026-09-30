"""One-shot structured recommendations; workflow and approval stay with callers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel

from app.llm.factory import build_chat_model
from app.llm.json_response import parse_json_object, response_text

T = TypeVar("T")
OutputMethod = Literal["prompt_json", "json_schema", "function_calling"]


@dataclass(frozen=True)
class RecommendationResult(Generic[T]):
    raw_response: str
    parsed: T | None = None
    error: str | None = None
    model_id: str | None = None
    raw_message: dict[str, Any] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return self.error is None and self.parsed is not None


class StructuredRecommender(Generic[T]):
    """Reusable model boundary with a task-owned schema and normalizer.

    Subclasses may provide prompts and business validation. A forced function
    call submits structured data; this class never executes a model-named tool.
    Legacy sparse contracts can retain prompt JSON and their existing parser.
    """

    def __init__(
        self,
        parse: Callable[[dict[str, Any]], T],
        *,
        schema: type[BaseModel] | Mapping[str, Any] | None = None,
        method: OutputMethod = "prompt_json",
    ) -> None:
        if method not in {"prompt_json", "json_schema", "function_calling"}:
            raise ValueError(f"unsupported structured output method: {method}")
        if method != "prompt_json" and schema is None:
            raise ValueError("structured output requires a response schema")
        self.parse = parse
        self.schema = schema.model_json_schema() if isinstance(schema, type) else schema
        self.method = method

    def invoke(
        self,
        messages: Any,
        *,
        model_factory: Callable[[], Any] | None = None,
    ) -> RecommendationResult[T]:
        raw = ""
        raw_message: dict[str, Any] = {}
        model_id = None
        try:
            model = (model_factory or build_chat_model)()
            original_model_id = type(model).__name__
            if self.method == "json_schema":
                model = model.bind(
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "recommendation",
                            "schema": self.schema,
                            "strict": True,
                        },
                    }
                )
            elif self.method == "function_calling":
                model = model.bind(
                    tools=[
                        {
                            "type": "function",
                            "function": {
                                "name": "submit_recommendation",
                                "description": "Submit a proposal for review.",
                                "parameters": self.schema,
                                "strict": True,
                            },
                        }
                    ],
                    tool_choice={
                        "type": "function",
                        "function": {
                            "name": "submit_recommendation",
                        },
                    },
                    parallel_tool_calls=False,
                )
            response = model.invoke(messages)
            model_id = original_model_id
            if isinstance(response, BaseModel):
                raw_message = response.model_dump(mode="json")
            raw = response_text(response)
            if self.method == "function_calling":
                calls = getattr(response, "additional_kwargs", {}).get("tool_calls")
                if not isinstance(calls, list) or len(calls) != 1:
                    raise ValueError(
                        "expected exactly one recommendation function call"
                    )
                function = calls[0].get("function", {})
                if function.get("name") != "submit_recommendation":
                    raise ValueError("unexpected recommendation function")
                raw = function.get("arguments")
                if not isinstance(raw, str):
                    raise ValueError("function arguments must be a JSON string")
            parsed = self.parse(parse_json_object(raw))
            return RecommendationResult(raw, parsed, None, model_id, raw_message)
        except Exception as exc:
            return RecommendationResult(
                raw if isinstance(raw, str) else "",
                None,
                f"{type(exc).__name__}: {exc}",
                model_id,
                raw_message,
            )
