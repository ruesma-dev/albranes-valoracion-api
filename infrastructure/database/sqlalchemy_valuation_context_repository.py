# infrastructure/database/sqlalchemy_valuation_context_repository.py
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from domain.ports.valuation_context_repository import (
    RawAlbaranLine,
    RawContratoHeader,
    RawContratoLine,
    ValuationContextRaw,
    ValuationContextRepository,
)
from infrastructure.database.session_factory import SessionFactory

logger = logging.getLogger(__name__)


# SQL crudo: no reutilizamos los ORM del servicio 3 para no acoplar
# este microservicio a su código. Esto es una VIEW conceptual sobre
# las tablas públicas del modelo de persistencia.

_SQL_MERGE_HEADER = text(
    """
    SELECT
        id AS document_id,
        proveedor_cif AS cif_proveedor,
        proveedor_nombre AS nombre_proveedor,
        obra_codigo AS codigo_obra,
        obra_nombre AS nombre_obra,
        selected_contrato_codigo AS codigo_contrato_seleccionado
    FROM albaran_documents_merge
    WHERE id = :document_id
    """
)

# -----------------------------------------------------------------
# Tras la sub-tanda 2A, la tabla albaran_lines_merge tiene dos
# columnas nuevas:
#   - unidad_medida (VARCHAR 32) — la unidad real extraída por el OCR.
#   - contexto_linea_json (TEXT) — JSON con la familia/rol/etc.
#     (hormigón, combustible, alquiler), o NULL si la línea es
#     de producto simple.
# Las leemos aquí. El prefilter del svc5:
#   - clasifica unidad_medida en UnitCategory,
#   - deserializa contexto_linea_json a ContextoLinea.
#
# COMPATIBILIDAD: para albaranes persistidos ANTES de la sub-tanda 2A
# las dos columnas vienen NULL → unidad_categoria='unknown' y
# contexto_linea=None, que es el comportamiento previo. No rompe.
#
# -----------------------------------------------------------------
# Tanda descuento — abr 2026:
#
# Se añaden al SELECT las columnas ``descuento`` y ``precio_neto``
# de ``albaran_lines_merge``. Viajan por el envelope hasta el svc6
# donde se usan para calcular el importe valorado con descuento.
#
# NOTA sobre el alias existente ``precio_neto AS importe_albaran``:
# Este alias viene de ANTES de la tanda descuento. Lo que el svc5
# llamaba "importe_albaran" era en realidad el precio_neto unitario
# de la línea (no el importe total = cantidad × precio). Esto no
# cambia: el alias se mantiene tal cual para no romper aguas abajo.
# Los campos NUEVOS (descuento_albaran, precio_neto_albaran) se
# leen con alias propios y explícitos.
#
# Compatibilidad: para albaranes sin columna descuento (anteriores
# al fix de svc3), la columna viene NULL → descuento=None en el
# DTO → el svc6 lo interpreta como "sin descuento" → fórmula sin
# cambios.
# -----------------------------------------------------------------
_SQL_ALBARAN_LINES = text(
    """
    SELECT
        id                  AS merge_line_id,
        line_index          AS line_index,
        codigo              AS codigo,
        concepto            AS descripcion,
        unidad_medida       AS unidad_medida,
        cantidad            AS cantidad,
        precio              AS precio_unitario_albaran,
        precio_neto         AS importe_albaran,
        codigo_imputacion   AS codigo_partida_albaran,
        contexto_linea_json AS contexto_linea_json,
        descuento           AS descuento_albaran,
        precio_neto         AS precio_neto_albaran
    FROM albaran_lines_merge
    WHERE document_id = :document_id
    ORDER BY line_index
    """
)


_SQL_CONTRATO_HEADER = text(
    """
    SELECT
        id,
        codigo_contrato,
        nombre_contrato,
        cif_proveedor,
        nombre_proveedor,
        codigo_obra,
        nombre_obra,
        pdf_sharepoint_relative_path,
        pdf_sharepoint_web_url
    FROM albaran_contratos_merge
    WHERE codigo_contrato = :codigo_contrato
    ORDER BY id DESC
    LIMIT 1
    """
)

_SQL_CONTRATO_LINES = text(
    """
    SELECT
        cl.id                AS contrato_line_id,
        cl.codigo_contrato   AS codigo_contrato,
        cl.codigo_producto   AS codigo_producto,
        cl.descripcion_linea AS descripcion,
        cl.unidad_medida     AS unidad_medida,
        cl.precio_unitario   AS precio_unitario,
        cl.codigo_partida    AS codigo_partida
    FROM albaran_contrato_lines_merge cl
    WHERE cl.contrato_id = :contrato_id
    ORDER BY cl.linea NULLS LAST, cl.id
    """
)


class SqlAlchemyValuationContextRepository(ValuationContextRepository):
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def load_context(
        self,
        *,
        document_id: str,
        codigo_contrato_override: str | None = None,
    ) -> ValuationContextRaw:
        with self._session_factory.create_session() as session:
            header_row = session.execute(
                _SQL_MERGE_HEADER,
                {"document_id": document_id},
            ).mappings().first()
            if header_row is None:
                raise KeyError(
                    f"Documento merge no encontrado: {document_id}"
                )

            codigo_contrato = (
                codigo_contrato_override
                or header_row.get("codigo_contrato_seleccionado")
            )

            albaran_rows = session.execute(
                _SQL_ALBARAN_LINES,
                {"document_id": document_id},
            ).mappings().all()

            lineas_albaran = [
                self._build_albaran_line(row) for row in albaran_rows
            ]

            if not codigo_contrato:
                logger.warning(
                    "document_id=%s sin contrato seleccionado. "
                    "Devolviendo contexto sin contrato.",
                    document_id,
                )
                return ValuationContextRaw(
                    document_id=document_id,
                    codigo_contrato_seleccionado=None,
                    contrato=None,
                    lineas_albaran=lineas_albaran,
                    lineas_contrato=[],
                )

            # La cabecera del contrato se busca por CODIGO (no por
            # document_id): es unica por sigrid_ide y su document_id es el
            # del ultimo albaran enriquecido, asi que los albaranes que
            # COMPARTEN contrato no la encontraban por document_id.
            contrato_row = session.execute(
                _SQL_CONTRATO_HEADER,
                {"codigo_contrato": codigo_contrato},
            ).mappings().first()

            if contrato_row is None:
                raise KeyError(
                    f"Contrato no encontrado para document_id={document_id} "
                    f"codigo_contrato={codigo_contrato}"
                )

            # Las lineas se atan al id de ESA cabecera (contrato_id), no al
            # document_id, para que coincidan con la cabecera elegida.
            contrato_lines_rows = session.execute(
                _SQL_CONTRATO_LINES,
                {"contrato_id": contrato_row["id"]},
            ).mappings().all()

            lineas_contrato = [
                self._build_contrato_line(row) for row in contrato_lines_rows
            ]

            return ValuationContextRaw(
                document_id=document_id,
                codigo_contrato_seleccionado=codigo_contrato,
                contrato=self._build_contrato_header(contrato_row),
                lineas_albaran=lineas_albaran,
                lineas_contrato=lineas_contrato,
            )

    @staticmethod
    def _build_albaran_line(row: dict[str, Any]) -> RawAlbaranLine:
        return RawAlbaranLine(
            merge_line_id=int(row["merge_line_id"]),
            line_index=int(row["line_index"] or 0),
            codigo=_opt_str(row.get("codigo")),
            descripcion=_opt_str(row.get("descripcion")),
            unidad_medida=_opt_str(row.get("unidad_medida")),
            cantidad=_opt_float(row.get("cantidad")),
            precio_unitario_albaran=_opt_float(row.get("precio_unitario_albaran")),
            importe_albaran=_opt_float(row.get("importe_albaran")),
            codigo_partida_albaran=_opt_str(row.get("codigo_partida_albaran")),
            contexto_linea_json=_opt_str(row.get("contexto_linea_json")),
            # Tanda descuento — abr 2026
            descuento=_opt_float(row.get("descuento_albaran")),
            precio_neto=_opt_float(row.get("precio_neto_albaran")),
        )

    @staticmethod
    def _build_contrato_line(row: dict[str, Any]) -> RawContratoLine:
        return RawContratoLine(
            contrato_line_id=int(row["contrato_line_id"]),
            codigo_contrato=str(row["codigo_contrato"]),
            codigo_producto=_opt_str(row.get("codigo_producto")),
            descripcion=_opt_str(row.get("descripcion")),
            unidad_medida=_opt_str(row.get("unidad_medida")),
            precio_unitario=_opt_float(row.get("precio_unitario")),
            codigo_partida=_opt_str(row.get("codigo_partida")),
        )

    @staticmethod
    def _build_contrato_header(row: dict[str, Any]) -> RawContratoHeader:
        return RawContratoHeader(
            codigo_contrato=str(row["codigo_contrato"]),
            nombre_contrato=_opt_str(row.get("nombre_contrato")),
            cif_proveedor=_opt_str(row.get("cif_proveedor")),
            nombre_proveedor=_opt_str(row.get("nombre_proveedor")),
            codigo_obra=_opt_str(row.get("codigo_obra")),
            nombre_obra=_opt_str(row.get("nombre_obra")),
            pdf_relative_path=_opt_str(row.get("pdf_sharepoint_relative_path")),
            pdf_web_url=_opt_str(row.get("pdf_sharepoint_web_url")),
        )


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
