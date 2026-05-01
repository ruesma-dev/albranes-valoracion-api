# domain/ports/valuation_context_repository.py
"""Puerto para leer el contexto de valoración de BBDD.

Reconstruye una "vista" agregada desde las tablas del svc3:
  - ``albaran_documents_merge`` / ``albaran_lines_merge``
  - ``albaran_contratos_merge`` / ``albaran_contrato_lines_merge``

No usa el ORM del svc3 — solo SQL crudo con ``sqlalchemy.text``.
Ver la implementación en
``infrastructure/database/sqlalchemy_valuation_context_repository.py``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class RawAlbaranLine:
    """Una línea de albarán leída crudo de BBDD (sin categorizar)."""

    merge_line_id: int
    line_index: int
    codigo: Optional[str]
    descripcion: Optional[str]
    # -----------------------------------------------------------------
    # Unidad de medida tal cual la dejó el OCR en
    # ``albaran_lines_merge.unidad_medida``. Puede ser None si
    # (a) el albarán es antiguo y se persistió antes de la sub-tanda 2A,
    # (b) el OCR no supo extraerla.
    # El prefilter del svc5 la clasificará en UnitCategory.
    # -----------------------------------------------------------------
    unidad_medida: Optional[str]
    cantidad: Optional[float]
    precio_unitario_albaran: Optional[float]
    importe_albaran: Optional[float]
    codigo_partida_albaran: Optional[str]
    # -----------------------------------------------------------------
    # Contenido crudo del JSON persistido en
    # ``albaran_lines_merge.contexto_linea_json``. Null cuando la línea
    # no es de familia compleja. El prefilter lo deserializa a
    # ``ContextoLinea``.
    # -----------------------------------------------------------------
    contexto_linea_json: Optional[str] = None

    # -----------------------------------------------------------------
    # Tanda descuento — abr 2026
    #
    # Descuento porcentual (0-100) y precio neto unitario de la línea
    # tal como están en albaran_lines_merge. Se propagan al
    # AlbaranLineForValuation → envelope → svc6 para que el builder
    # aplique el descuento al calcular el importe valorado.
    #
    # Compatibilidad retroactiva: ambos con default None. Albaranes
    # anteriores a la tanda descuento / sin descuento llegan como
    # None → el builder los interpreta como "sin descuento" y la
    # fórmula se comporta como antes.
    # -----------------------------------------------------------------
    descuento: Optional[float] = None
    precio_neto: Optional[float] = None


@dataclass(frozen=True)
class RawContratoLine:
    """Una línea de contrato leída crudo de BBDD (sin categorizar)."""

    contrato_line_id: int
    codigo_contrato: str
    codigo_producto: Optional[str]
    descripcion: Optional[str]
    unidad_medida: Optional[str]
    precio_unitario: Optional[float]
    codigo_partida: Optional[str]


@dataclass(frozen=True)
class RawContratoHeader:
    """Cabecera del contrato (con datos para montar el contexto)."""

    codigo_contrato: str
    nombre_contrato: Optional[str]
    cif_proveedor: Optional[str]
    nombre_proveedor: Optional[str]
    codigo_obra: Optional[str]
    nombre_obra: Optional[str]
    pdf_relative_path: Optional[str]
    pdf_web_url: Optional[str]


@dataclass(frozen=True)
class ValuationContextRaw:
    """DTO que devuelve el repositorio al pipeline."""

    document_id: str
    codigo_contrato_seleccionado: Optional[str]
    contrato: Optional[RawContratoHeader]
    lineas_albaran: list[RawAlbaranLine] = field(default_factory=list)
    lineas_contrato: list[RawContratoLine] = field(default_factory=list)


class ValuationContextRepository(ABC):
    """Puerto para cargar el contexto de valoración desde la BBDD."""

    @abstractmethod
    def load_context(
        self,
        *,
        document_id: str,
        codigo_contrato_override: Optional[str] = None,
    ) -> ValuationContextRaw:
        """Carga el albarán y sus líneas de contrato asociado.

        Si ``codigo_contrato_override`` se pasa, se ignora el
        ``selected_contrato_codigo`` de la cabecera merge. Esto se usa
        desde el front cuando el revisor quiere re-valorar con otro
        contrato distinto al seleccionado.
        """
        ...
