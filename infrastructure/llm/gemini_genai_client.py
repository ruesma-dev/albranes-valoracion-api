# infrastructure/llm/gemini_genai_client.py
from __future__ import annotations

import json
import logging
import time
import traceback
from typing import Any, Optional, Type

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
        # Logger best-effort (puede ser None si IA_LOGGING_ENABLED=false).
        self._call_logger = call_logger

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
            "Gemini valuation call. model=%s kind=%s filename=%s size=%s "
            "schema=%s",
            model, att_kind, att_filename, att_size, response_model.__name__,
        )
        response_schema = response_model.model_json_schema()

        # Construcción de contents según haya o no adjunto.
        # Antes de esta tanda, el servicio inventaba un PDF dummy de
        # 15 bytes (b"%PDF-1.4\n%%EOF\n") cuando no había PDF real.
        # Gemini lo rechazaba con 400 INVALID_ARGUMENT: "The document
        # has no pages." Ahora si attachment es None, simplemente NO
        # añadimos la Part del PDF al contents — solo texto puro.
        contents: list[Any] = []
        if attachment is not None:
            contents.append(
                types.Part.from_bytes(
                    data=attachment.data,
                    mime_type=attachment.mime_type,
                )
            )
        contents.append(user_text)

        # Resumen del request (sin bytes binarios) para call_logger.
        request_summary = self._build_request_summary(
            model=model,
            instructions=instructions,
            user_text=user_text,
            attachment=attachment,
            schema_name=response_model.__name__,
            response_schema=response_schema,
        )

        response = None
        error_str: str | None = None
        t0 = time.time()
        try:
            response = run_with_retry(
                provider="gemini",
                operation=lambda: self._client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=instructions,
                        response_mime_type="application/json",
                        response_json_schema=response_schema,
                    ),
                ),
                policy=self._retry_policy,
            )
        except Exception as exc:
            error_str = (
                f"{type(exc).__name__}: {exc}\n"
                f"{traceback.format_exc(limit=3)}"
            )
            self._safe_log_call(
                model=model,
                request_summary=request_summary,
                response_payload=None,
                error=error_str,
                duration_ms=int((time.time() - t0) * 1000),
            )
            raise

        duration_ms = int((time.time() - t0) * 1000)

        try:
            parsed_obj = self._parse_response(response, response_model)
        except Exception as exc:
            error_str = f"PARSE ERROR: {type(exc).__name__}: {exc}"
            self._safe_log_call(
                model=model,
                request_summary=request_summary,
                response_payload=response,
                error=error_str,
                duration_ms=duration_ms,
            )
            raise

        self._safe_log_call(
            model=model,
            request_summary=request_summary,
            response_payload={
                "raw_sdk_response": response,
                "parsed_pydantic": (
                    parsed_obj.model_dump(mode="json")
                    if isinstance(parsed_obj, BaseModel) else None
                ),
                "duration_ms": duration_ms,
            },
            error=None,
            duration_ms=duration_ms,
        )
        return parsed_obj

    # ------------------------------------------------------------ #
    # Helpers internos.
    # ------------------------------------------------------------ #
    def _safe_log_call(
        self,
        *,
        model: str,
        request_summary: dict,
        response_payload: Any,
        error: str | None,
        duration_ms: int,
    ) -> None:
        if self._call_logger is None:
            return
        try:
            request_summary["duration_ms"] = duration_ms
            self._call_logger.log_call(
                provider="gemini",
                model=model,
                request_summary=request_summary,
                response_payload=response_payload,
                error=error,
            )
        except Exception:
            logger.warning(
                "GeminiGenAiVisionClient: error guardando call log; "
                "se continúa.",
                exc_info=True,
            )

    @staticmethod
    def _build_request_summary(
        *,
        model: str,
        instructions: str,
        user_text: str,
        attachment: Optional[LlmAttachment],
        schema_name: str,
        response_schema: dict,
    ) -> dict:
        if attachment is None:
            attachment_summary = {"kind": "text_only"}
        else:
            attachment_summary = LlmCallLogger.attachment_summary(
                kind=attachment.kind,
                filename=attachment.filename,
                mime_type=attachment.mime_type,
                data=attachment.data,
            )
        return {
            "model": model,
            "instructions": instructions,
            "user_text": user_text,
            "attachment": attachment_summary,
            "schema_name": schema_name,
            "response_format": "json_schema",
            "response_schema_keys": list((response_schema or {}).keys()),
        }

    def _parse_response(
        self,
        response: Any,
        response_model: Type[BaseModel],
    ) -> BaseModel:
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
