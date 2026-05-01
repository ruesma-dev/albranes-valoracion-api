# domain/models/valuation_context.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from domain.models.contexto_linea import ContextoLinea

UnitCategory = Literal[
    "mass",
    "volume",
    "length",
    "area",
    "count",
    "time",
    "lump_sum",
    "unknown",
]


@dataclass(frozen=True)
class AlbaranLineForValuation:
    """Línea de albarán tal como entra al prompt IA.

    ``unidad_categoria`` se rellena por el pre-filtro determinista del
    servicio 5 (``UnitCategoryPrefilter``) ANTES de llamar a la IA.
    La IA NO deduce categorías — las recibe ya calculadas para limitar
    candidatos de matching.

    ``contexto_linea`` llega deserializado de
    ``albaran_lines_merge.contexto_linea_json``. Null si la línea no
    es de familia compleja. El prompt V2 del svc5 lo consume para
    aplicar las reglas Forma A/B/C de matching de modificadores.

    -------------------------------------------------------------------
    Tanda descuento (abr 2026):
    -------------------------------------------------------------------
    Se añaden ``descuento_albaran`` y ``precio_neto_albaran`` para que
    el descuento llegue del merge (svc3) al builder (svc6) a través
    del envelope de valoración.

    El bug original: el LLM valoraba con ``cantidad × precio`` sin
    aplicar el descuento del albarán. Con estos campos en el contexto:
      - El LLM los ve como información de la línea (puede usarlos para
        razonar sobre las observaciones).
      - El svc6 los lee del envelope y aplica el descuento al importe
        valorado: ``importe = cantidad × precio_contrato × (1 - d/100)``.

    Decisión de negocio (Construcciones Ruesma):
      - El descuento del albarán se aplica al precio del CONTRATO
        cuando se calcula el importe valorado.
      - Las líneas sintéticas (M1-M7) heredan el descuento de su línea
        base padre.
    -------------------------------------------------------------------
    """

    merge_line_id: int
    line_index: int
    codigo: Optional[str]
    descripcion: Optional[str]
    unidad_medida: Optional[str]
    unidad_categoria: UnitCategory
    cantidad: Optional[float]
    precio_unitario_albaran: Optional[float]
    importe_albaran: Optional[float]
    codigo_partida_albaran: Optional[str]
    contexto_linea: Optional[ContextoLinea] = None
    # Tanda descuento — abr 2026
    descuento_albaran: Optional[float] = None
    precio_neto_albaran: Optional[float] = None


@dataclass(frozen=True)
class ContratoLineForValuation:
    """Línea de contrato (de BBDD Sigrid) que entra al prompt IA."""

    contrato_line_id: int
    codigo_contrato: str
    codigo_producto: Optional[str]
    descripcion: Optional[str]
    unidad_medida: Optional[str]
    unidad_categoria: UnitCategory
    precio_unitario: Optional[float]
    codigo_partida: Optional[str]


@dataclass(frozen=True)
class ContextoValoracion:
    """Conjunto de datos que el servicio 5 monta para llamar a la IA."""

    document_id: str
    codigo_contrato: str
    nombre_contrato: Optional[str]
    cif_proveedor: Optional[str]
    nombre_proveedor: Optional[str]
    codigo_obra: Optional[str]
    nombre_obra: Optional[str]
    pdf_relative_path: Optional[str]
    pdf_filename: Optional[str]
    lineas_albaran: list[AlbaranLineForValuation]
    lineas_contrato: list[ContratoLineForValuation]
