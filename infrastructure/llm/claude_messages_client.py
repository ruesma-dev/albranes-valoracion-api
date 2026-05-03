# infrastructure/llm/claude_messages_client.py
from __future__ import annotations

import base64
import json
import logging
from typing import Any, Optional, Type

import anthropic
from pydantic import BaseModel

from domain.models.llm_attachment import LlmAttachment
from domain.ports.llm_client import LlmVisionClient
from infrastructure.llm.retry_policy import RetryPolicy, run_with_retry

logger = logging.getLogger(__name__)

_TOOL_NAME = "emit_valuation_result"
_TOOL_DESCRIPTION = (
    "Devuelve la valoración estructurada de las líneas del albarán "
    "conforme al esquema exigido. Llama SIEMPRE y SOLO a esta herramienta."
)


class ClaudeMessagesVisionClient(LlmVisionClient):
    def __init__(
        self,
        *,
        api_key: str,
        max_tokens: int = 8192,
        timeout_s: int = 180,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._client = anthropic.Anthropic(
            api_key=api_key,
            timeout=float(timeout_s),
        )
        self._max_tokens = int(max_tokens)
        self._retry_policy = retry_policy or RetryPolicy()

    @staticmethod
    def _sanitize_schema(schema: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(schema, dict):
            return schema
        cleaned = {k: v for k, v in schema.items() if k != "title"}
        if "properties" in cleaned and isinstance(cleaned["properties"], dict):
            cleaned["properties"] = {
                key: ClaudeMessagesVisionClient._sanitize_schema(value)
                for key, value in cleaned["properties"].items()
            }
        if "$defs" in cleaned and isinstance(cleaned["$defs"], dict):
            cleaned["$defs"] = {
                key: ClaudeMessagesVisionClient._sanitize_schema(value)
                for key, value in cleaned["$defs"].items()
            }
        if "items" in cleaned and isinstance(cleaned["items"], dict):
            cleaned["items"] = ClaudeMessagesVisionClient._sanitize_schema(
                cleaned["items"]
            )
        return cleaned

    @staticmethod
    def _b64(data: bytes) -> str:
        return base64.b64encode(data).decode("utf-8")

    def _build_content_block(
        self,
        *,
        attachment: Optional[LlmAttachment],
        user_text: str,
    ) -> list[dict[str, Any]]:
        # Modo texto puro (sin adjunto): para valoración sin PDF de
        # contrato (solo fase 1a sobre la tabla del ERP).
        if attachment is None:
            return [{"type": "text", "text": user_text}]

        if attachment.kind == "pdf":
            document_block: dict[str, Any] = {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": self._b64(attachment.data),
                },
            }
            return [document_block, {"type": "text", "text": user_text}]

        image_block: dict[str, Any] = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": attachment.mime_type or "image/jpeg",
                "data": self._b64(attachment.data),
            },
        }
        return [image_block, {"type": "text", "text": user_text}]

    def extract_document(
        self,
        *,
        model: str,
        instructions: str,
        user_text: str,
        attachment: Optional[LlmAttachment] = None,
        response_model: Type[BaseModel],
    ) -> BaseModel:
        att_kind = attachment.kind if attachment is not None else "text_only"
        att_filename = attachment.filename if attachment is not None else "n/a"
        att_size = len(attachment.data) if attachment is not None else 0
        logger.info(
            "Claude valuation call. model=%s kind=%s filename=%s size=%s "
            "schema=%s user_text_len=%s",
            model, att_kind, att_filename, att_size,
            response_model.__name__, len(user_text),
        )
        raw_schema = response_model.model_json_schema()
        input_schema = self._sanitize_schema(raw_schema)
        tool_spec = {
            "name": _TOOL_NAME,
            "description": _TOOL_DESCRIPTION,
            "input_schema": input_schema,
        }
        content = self._build_content_block(
            attachment=attachment, user_text=user_text,
        )
        response = run_with_retry(
            provider="claude",
            operation=lambda: self._client.messages.create(
                model=model,
                max_tokens=self._max_tokens,
                system=instructions,
                tools=[tool_spec],
                tool_choice={"type": "tool", "name": _TOOL_NAME},
                messages=[{"role": "user", "content": content}],
            ),
            policy=self._retry_policy,
        )
        for block in response.content or []:
            if getattr(block, "type", None) != "tool_use":
                continue
            if getattr(block, "name", None) != _TOOL_NAME:
                continue
            tool_input = getattr(block, "input", None)
            if isinstance(tool_input, dict):
                return response_model.model_validate(tool_input)
            if isinstance(tool_input, str):
                return response_model.model_validate_json(tool_input)

        text_parts: list[str] = []
        for block in response.content or []:
            if getattr(block, "type", None) == "text":
                text_value = getattr(block, "text", None)
                if isinstance(text_value, str) and text_value.strip():
                    text_parts.append(text_value)
        if text_parts:
            joined = "\n".join(text_parts).strip()
            try:
                payload = json.loads(self._strip_code_fences(joined))
                if isinstance(payload, dict):
                    return response_model.model_validate(payload)
            except Exception as exc:
                logger.debug(
                    "Claude devolvió texto no parseable como JSON: %s", exc
                )
        raise ValueError(
            "Claude no devolvió tool_use ni JSON válido en la respuesta."
        )

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
        return cleaned
