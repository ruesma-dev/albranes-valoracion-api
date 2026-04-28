# infrastructure/llm/llm_call_logger.py
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class LlmCallLogger:
    """Persiste a disco request/response de cada llamada al LLM.

    Pensado para auditoría y debug. Es totalmente DEFENSIVO:
    cualquier error de I/O se loguea como warning pero NUNCA propaga
    (la llamada al LLM no se rompe por culpa del logger).

    Diseño:
      - Carpeta base configurable; si es None o cadena vacía, logging
        deshabilitado y todas las llamadas a log_call() son no-op.
      - Particionado por día para evitar megacarpetas:
          <base_dir>/<YYYYMMDD>/<HHMMSS_milis>_<provider>_<short_id>.json
      - El attachment binario (PDF, imagen) NUNCA se guarda crudo.
        Solo metadatos + sha256 para correlación con otros logs.
      - El response del SDK se serializa best-effort:
          1. obj.model_dump() si es Pydantic v2.
          2. obj.to_dict() si lo expone (algunos SDKs).
          3. dict / list / primitivos pasan tal cual.
          4. vars(obj) si tiene __dict__.
          5. str(obj) como último recurso.

    Uso típico (composition root):
        logger_io = LlmCallLogger(base_dir=settings.LLM_CALL_LOG_DIR)
        client = ClaudeMessagesVisionClient(
            api_key=...,
            call_logger=logger_io,
        )
    """

    def __init__(self, base_dir: Path | str | None) -> None:
        if base_dir is None or str(base_dir).strip() == "":
            self._base_dir: Path | None = None
            return
        self._base_dir = Path(base_dir)
        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning(
                "LlmCallLogger: no se pudo crear directorio base %s: %s. "
                "Logging desactivado.",
                self._base_dir, exc,
            )
            self._base_dir = None

    @property
    def enabled(self) -> bool:
        return self._base_dir is not None

    def log_call(
        self,
        *,
        provider: str,
        model: str,
        request_summary: dict[str, Any],
        response_payload: Any = None,
        error: str | None = None,
        document_id: str | None = None,
    ) -> Path | None:
        """Escribe un JSON con la trazabilidad de una llamada al LLM.

        :param provider: 'claude' | 'openai' | 'gemini'.
        :param model: nombre del modelo concreto.
        :param request_summary: dict serializable con instructions,
            user_text, attachment metadata (NO bytes), tool_spec, etc.
        :param response_payload: objeto SDK del LLM o None si falló.
        :param error: traceback corto si la llamada lanzó excepción.
        :param document_id: id en BBDD para correlacionar con logs.
        :return: Path del fichero escrito o None si deshabilitado/falló.
        """
        if self._base_dir is None:
            return None
        try:
            now = datetime.now(timezone.utc)
            day_dir = self._base_dir / now.strftime("%Y%m%d")
            day_dir.mkdir(parents=True, exist_ok=True)
            ts = now.strftime("%H%M%S_") + f"{now.microsecond // 1000:03d}"
            short_id = (
                str(document_id)[:8]
                if document_id
                else uuid.uuid4().hex[:8]
            )
            filename = f"{ts}_{provider}_{short_id}.json"
            target = day_dir / filename
            payload = {
                "timestamp_utc": now.isoformat(),
                "provider": provider,
                "model": model,
                "document_id": document_id,
                "status": "error" if error else "ok",
                "error": error,
                "request": request_summary,
                "response": self._serializable(response_payload),
            }
            target.write_text(
                json.dumps(
                    payload, ensure_ascii=False, indent=2, default=str,
                ),
                encoding="utf-8",
            )
            return target
        except Exception as exc:
            logger.warning(
                "LlmCallLogger: error guardando llamada %s: %s",
                provider, exc,
            )
            return None

    @staticmethod
    def attachment_summary(
        *,
        kind: str,
        filename: str | None,
        mime_type: str | None,
        data: bytes,
    ) -> dict[str, Any]:
        """Metadatos seguros del attachment (sin bytes binarios)."""
        return {
            "kind": kind,
            "filename": filename,
            "mime_type": mime_type,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    @classmethod
    def _serializable(cls, obj: Any) -> Any:
        """Convierte cualquier objeto SDK a algo JSON-serializable."""
        if obj is None:
            return None
        if isinstance(obj, (str, int, float, bool)):
            return obj
        if hasattr(obj, "model_dump"):
            try:
                return obj.model_dump(mode="json")
            except Exception:
                try:
                    return obj.model_dump()
                except Exception:
                    pass
        if hasattr(obj, "to_dict"):
            try:
                return obj.to_dict()
            except Exception:
                pass
        if isinstance(obj, dict):
            return {k: cls._serializable(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [cls._serializable(v) for v in obj]
        if hasattr(obj, "__dict__"):
            try:
                return {
                    k: cls._serializable(v)
                    for k, v in vars(obj).items()
                    if not k.startswith("_")
                }
            except Exception:
                pass
        return str(obj)
