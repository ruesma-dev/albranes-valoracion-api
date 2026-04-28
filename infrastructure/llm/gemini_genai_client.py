# infrastructure/llm/gemini_genai_client.py
from __future__ import annotations

import json
import logging
from typing import Any, Type

from google import genai
from google.genai import types
from pydantic import BaseModel

from domain.models.llm_attachment import LlmAttachment
from domain.ports.llm_client import LlmVisionClient
from infrastructure.llm.llm_call_logger import LlmCallLogger
from infrastructure.llm.retry_policy import RetryPolicy, run_with_retry

logger = logging.getLogger(__name__)


class GeminiGenAiVisionClient(LlmVisionClient):
    def __init__(
        self,
        api_key: str,
        *,
        retry_policy: RetryPolicy | None = None,
        call_logger: LlmCallLogger | None = None,
    ) -> None:
        self._client = genai.Client(api_key=api_key)
        self._retry_policy = retry_policy or RetryPolicy()
        self._call_logger = call_logger or LlmCallLogger(base_dir=None)

    def extract_document(
        self,
        *,
        model: str,
        instructions: str,
        user_text: str,
        attachment: LlmAttachment,
        response_model: Type[BaseModel],
    ) -> BaseModel:
        logger.info(
            "Gemini valuation call. model=%s kind=%s filename=%s size=%s "
            "schema=%s",
            model, attachment.kind, attachment.filename,
            len(attachment.data), response_model.__name__,
        )
        response_schema = response_model.model_json_schema()

        request_summary = {
            "instructions": instructions,
            "user_text": user_text,
            "attachment": LlmCallLogger.attachment_summary(
                kind=attachment.kind,
                filename=attachment.filename,
                mime_type=attachment.mime_type,
                data=attachment.data,
            ),
            "schema": response_model.__name__,
        }

        try:
            response = run_with_retry(
                provider="gemini",
                operation=lambda: self._client.models.generate_content(
                    model=model,
                    contents=[
                        types.Part.from_bytes(
                            data=attachment.data,
                            mime_type=attachment.mime_type,
                        ),
                        user_text,
                    ],
                    config=types.GenerateContentConfig(
                        system_instruction=instructions,
                        response_mime_type="application/json",
                        response_json_schema=response_schema,
                    ),
                ),
                policy=self._retry_policy,
            )
        except Exception as exc:
            self._call_logger.log_call(
                provider="gemini",
                model=model,
                request_summary=request_summary,
                response_payload=None,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

        self._call_logger.log_call(
            provider="gemini",
            model=model,
            request_summary=request_summary,
            response_payload=response,
        )

        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, response_model):
            return parsed
        if isinstance(parsed, BaseModel):
            return response_model.model_validate(parsed.model_dump())
        if isinstance(parsed, dict):
            return response_model.model_validate(parsed)

        response_text = getattr(response, "text", None)
        if isinstance(response_text, str) and response_text.strip():
            return response_model.model_validate_json(
                self._sanitize_json_text(response_text)
            )

        parsed_from_candidates = self._extract_json_dict(response)
        if parsed_from_candidates is not None:
            return response_model.model_validate(parsed_from_candidates)

        raise ValueError("Gemini no devolvió parsed ni text JSON válido.")

    @staticmethod
    def _sanitize_json_text(text: str) -> str:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if len(lines) >= 2:
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                cleaned = "\n".join(lines).strip()
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
        return cleaned

    @classmethod
    def _extract_json_dict(cls, response: Any) -> dict[str, Any] | None:
        candidates = getattr(response, "candidates", None) or []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            parts = getattr(content, "parts", None) or []
            for part in parts:
                text = getattr(part, "text", None)
                if isinstance(text, str) and text.strip():
                    try:
                        loaded = json.loads(cls._sanitize_json_text(text))
                    except Exception:
                        continue
                    if isinstance(loaded, dict):
                        return loaded
        return None
