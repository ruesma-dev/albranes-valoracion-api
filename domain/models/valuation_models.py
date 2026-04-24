# domain/models/valuation_models.py
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import Field

from domain.models.schema_base import StrictSchemaModel


MatchMethod = Literal["exact_concept", "semantic", "price_only", "no_match"]


class LineValuation(StrictSchemaModel):
    """Output de la IA para una línea del albarán.

    La IA SOLO rellena los resultados de las fases 1a y 1b:
      - matched_contrato_line_id + precio_unitario_contrato_db (fase 1a)
      - precio_unitario_pdf_inferido + pdf_inference_reasoning (fase 1b)
      - categoría de unidad confirmada + match_confidence + razón

    NO rellena: partida final, cantidad convertida, importe calculado.
    Eso lo hace el servicio 6.
    """

    merge_line_id: int = Field(
        description="Id de la línea del albarán (albaran_lines_merge.id) "
                    "que se recibió en el input. Sirve para que el servicio "
                    "6 case la respuesta con el input original."
    )
    match_method: MatchMethod = Field(
        description="exact_concept | semantic | price_only | no_match"
    )
    matched_contrato_line_id: Optional[int] = Field(
        default=None,
        description="Id de la línea del contrato (albaran_contrato_lines"
                    "_merge.id) que la IA considera match. Null si no hay."
    )
    match_confidence_pct: float = Field(
        ge=0,
        le=100,
        description="Confianza 0-100 del matching hecho.",
    )
    unidad_categoria_albaran: str = Field(
        description="Categoría confirmada de la unidad del albarán: "
                    "mass|volume|length|area|count|time|lump_sum|unknown."
    )
    unidad_category_match: bool = Field(
        description="True solo si hay una línea de contrato con categoría "
                    "compatible Y fue casable. Si no, false."
    )
    precio_unitario_contrato_db: Optional[float] = Field(
        default=None,
        description="Precio unitario de la línea de contrato casada (fase 1a). "
                    "Si matched_contrato_line_id es null, este también lo es."
    )
    precio_unitario_pdf_inferido: Optional[float] = Field(
        default=None,
        description="Precio unitario leído directamente del PDF del "
                    "contrato (fase 1b). Null si no se ha podido deducir."
    )
    pdf_inference_reasoning: Optional[str] = Field(
        default=None,
        description="Explicación corta del lugar del PDF donde se ha "
                    "encontrado el precio inferido.",
    )
    razon_corta: str = Field(
        description="Explicación corta del matching hecho, en español, "
                    "para auditoría."
    )


class DocumentoValoracion(StrictSchemaModel):
    lineas: List[LineValuation]
