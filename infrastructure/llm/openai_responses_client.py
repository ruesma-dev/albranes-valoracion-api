# infrastructure/llm/openai_responses_client.py
from __future__ import annotations

import base64
import logging
import re
from typing import Type

from openai import OpenAI
from pydantic import BaseModel

from domain.models.llm_attachment import LlmAttachment
from domain.ports.llm_client import LlmVisionClient
from infrastructure.llm.openai_sdk_compat import patch_openai_pydantic_compat
from infrastructure.llm.retry_policy import RetryPolicy, run_with_retry

logger = logging.getLogger(__name__)


class OpenAIResponsesVisionClient(LlmVisionClient):
    def __init__(
        self,
        api_key: str,
        *,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        patch_openai_pydantic_compat()
        self._client = OpenAI(api_key=api_key)
        self._retry_policy = retry_policy or RetryPolicy()

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
        attachment: LlmAttachment,
        response_model: Type[BaseModel],
    ) -> BaseModel:
        logger.info(
            "OpenAI valuation call. model=%s kind=%s filename=%s size=%s "
            "schema=%s",
            model, attachment.kind, attachment.filename,
            len(attachment.data), response_model.__name__,
        )

        if attachment.kind == "pdf":
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

        if response.output_parsed is not None:
            return response.output_parsed
        if getattr(response, "output_text", None):
            return response_model.model_validate_json(response.output_text)
        raise ValueError("OpenAI no devolvió output_parsed ni output_text.")
