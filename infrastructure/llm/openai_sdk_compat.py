# infrastructure/llm/openai_sdk_compat.py
from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

_patch_lock = threading.Lock()
_is_patched = False


def patch_openai_pydantic_compat() -> None:
    global _is_patched
    if _is_patched:
        return
    with _patch_lock:
        if _is_patched:
            return
        try:
            from openai import _compat as openai_compat  # type: ignore
        except Exception as exc:
            logger.debug(
                "No se pudo importar openai._compat para aplicar el parche: %s",
                exc,
            )
            _is_patched = True
            return

        original_model_dump = getattr(openai_compat, "model_dump", None)
        if not callable(original_model_dump):
            logger.debug(
                "openai._compat.model_dump no disponible; se omite el parche."
            )
            _is_patched = True
            return

        def _patched_model_dump(model: Any, *args: Any, **kwargs: Any) -> Any:
            if kwargs.get("by_alias") is None:
                kwargs["by_alias"] = False
            return original_model_dump(model, *args, **kwargs)

        setattr(openai_compat, "model_dump", _patched_model_dump)
        _is_patched = True
        logger.info("Aplicado parche de compatibilidad OpenAI/Pydantic.")
