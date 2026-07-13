# domain/models/conciliacion_models.py
"""Modelos de salida de IA4 (conciliacion semantica).

IA4 se lanza DESPUES del matching determinista de sv6, EN LOTE, con las
lineas que quedaron sin casar (o casadas pero sin precio) y las lineas de
contrato de Sigrid. Su trabajo es, por significado, casar cada linea con
la linea de contrato correcta y traer su PRECIO UNITARIO. NO calcula
importes (el importe = unitario * (1 - descuento/100) * cantidad lo hace
sv6 de forma determinista); IA4 solo aporta el matched_contrato_line_id y
el precio_unitario_contrato_db.
"""
from __future__ import annotations

from typing import List, Optional

from domain.models.schema_base import StrictSchemaModel
from pydantic import Field

# Reutilizamos los mismos literales de metodo de match que la valoracion.
try:  # pragma: no cover - alias defensivo
    from domain.models.valuation_models import MatchMethod
except Exception:  # pragma: no cover
    from typing import Literal

    MatchMethod = Literal[  # type: ignore
        "exact_concept", "semantic", "price_only", "no_match"
    ]


class LineaConciliacion(StrictSchemaModel):
    """Resultado de conciliar UNA linea del albaran contra el contrato."""

    line_ref: int = Field(
        description=(
            "Referencia (indice) de la linea del lote de entrada que se "
            "concilia. Es el mismo valor que llego en la entrada. Sirve "
            "para lineas de albaran y para sinteticas por igual."
        ),
    )
    matched_contrato_line_id: Optional[int] = Field(
        default=None,
        description=(
            "id de la linea de la TABLA del contrato (Sigrid) con la que "
            "casa por significado. null si no hay match fiable."
        ),
    )
    precio_unitario_contrato_db: Optional[float] = Field(
        default=None,
        description=(
            "Precio UNITARIO de la linea de contrato casada (sin IVA). El "
            "importe NO se calcula aqui. null si no hay match."
        ),
    )
    match_method: MatchMethod = Field(
        default="no_match",
        description="exact_concept | semantic | price_only | no_match.",
    )
    match_confidence_pct: float = Field(
        default=0.0,
        ge=0.0,
        le=100.0,
        description="Confianza 0-100 del match.",
    )
    razon_corta: str = Field(
        description=(
            "Explicacion corta del match para auditoria (por que esa linea "
            "de contrato, mismo tipo/partida, etc.)."
        ),
    )


class DocumentoConciliacion(StrictSchemaModel):
    """Salida completa de IA4: una entrada por linea de entrada del lote."""

    conciliaciones: List[LineaConciliacion] = Field(
        default_factory=list,
        description=(
            "Una entrada por cada linea del lote de entrada (aunque sea "
            "no_match, para trazabilidad)."
        ),
    )
