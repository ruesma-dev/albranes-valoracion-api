# infrastructure/llm/openai_responses_client.py
from __future__ import annotations

import base64
import logging
import re
import time
import traceback
from typing import Any, Optional, Type

from openai import OpenAI
from pydantic import BaseModel

from domain.models.llm_attachment import LlmAttachment
from domain.ports.llm_client import LlmVisionClient
from infrastructure.llm.llm_call_logger import LlmCallLogger
from infrastructure.llm.openai_sdk_compat import patch_openai_pydantic_compat
from infrastructure.llm.retry_policy import RetryPolicy, run_with_retry

logger = logging.getLogger(__name__)


class OpenAIResponsesVisionClient(LlmVisionClient):
    def __init__(
        self,
        api_key: str,
        *,
        retry_policy: RetryPolicy | None = None,
        call_logger: LlmCallLogger | None = None,
    ) -> None:
        patch_openai_pydantic_compat()
        self._client = OpenAI(api_key=api_key)
        self._retry_policy = retry_policy or RetryPolicy()
        # Logger best-effort (puede ser None si IA_LOGGING_ENABLED=false).
        self._call_logger = call_logger

    @staticmethod
    def _to_data_url(mime_type: str, data: bytes) -> str:
        encoded = base64.b64encode(data).decode("utf-8")
        return f"data:{mime_type};base64,{encoded}"

    @staticmethod
    def _safe_filename(filename: str, fallback: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("_")
        return cleaned or fallback

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
            "OpenAI valuation call. model=%s kind=%s filename=%s size=%s "
            "schema=%s",
            model, att_kind, att_filename, att_size, response_model.__name__,
        )

        # Construcción del content según haya o no adjunto.
        if attachment is None:
            content = [{"type": "input_text", "text": user_text}]
        elif attachment.kind == "pdf":
            safe_name = self._safe_filename(attachment.filename, "contrato.pdf")
            content = [
                {
                    "type": "input_file",
                    "filename": safe_name,
                    "file_data": self._to_data_url(
                        "application/pdf", attachment.data,
                    ),
                },
                {"type": "input_text", "text": user_text},
            ]
        else:
            content = [
                {"type": "input_text", "text": user_text},
                {
                    "type": "input_image",
                    "image_url": self._to_data_url(
                        attachment.mime_type, attachment.data,
                    ),
                },
            ]

        # Resumen del request (sin bytes binarios) para call_logger.
        if attachment is None:
            attachment_summary: dict[str, Any] = {"kind": "text_only"}
        else:
            attachment_summary = LlmCallLogger.attachment_summary(
                kind=attachment.kind,
                filename=attachment.filename,
                mime_type=attachment.mime_type,
                data=attachment.data,
            )
        request_summary = {
            "model": model,
            "instructions": instructions,
            "user_text": user_text,
            "attachment": attachment_summary,
            "schema_name": response_model.__name__,
            "response_format": "responses.parse + text_format=Pydantic",
        }

        response = None
        error_str: str | None = None
        t0 = time.time()
        try:
            response = run_with_retry(
                provider="openai",
                operation=lambda: self._client.responses.parse(
                    model=model,
                    instructions=instructions,
                    input=[{"role": "user", "content": content}],
                    text_format=response_model,
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
                provider="openai",
                model=model,
                request_summary=request_summary,
                response_payload=response_payload,
                error=error,
            )
        except Exception:
            logger.warning(
                "OpenAIResponsesVisionClient: error guardando call log; "
                "se continúa.",
                exc_info=True,
            )

    @staticmethod
    def _parse_response(
        response: Any,
        response_model: Type[BaseModel],
    ) -> BaseModel:
        if response.output_parsed is not None:
            return response.output_parsed
        if getattr(response, "output_text", None):
            return response_model.model_validate_json(response.output_text)
        raise ValueError("OpenAI no devolvió output_parsed ni output_text.")
