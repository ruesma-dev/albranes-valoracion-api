# infrastructure/llm/gemini_genai_client.py
"""Reexport — la implementación canónica vive en ruesma-albaranes-comun.

Este módulo se conserva como reexport para que TODOS los imports del
servicio sigan funcionando sin tocar más ficheros, eliminando a la vez
la copia local divergente. Requiere: pip install -e ../comun
"""
from ruesma_comun.llm.gemini_genai_client import GeminiGenAiVisionClient

__all__ = ["GeminiGenAiVisionClient"]
