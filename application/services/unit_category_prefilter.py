# application/services/unit_category_prefilter.py
from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Iterable, Optional

from domain.models.contexto_linea import ContextoLinea
from domain.models.valuation_context import (
    AlbaranLineForValuation,
    ContratoLineForValuation,
    UnitCategory,
)
from domain.ports.valuation_context_repository import (
    RawAlbaranLine,
    RawContratoLine,
)

logger = logging.getLogger(__name__)


# Tabla de alias de unidades típicas de construcción.
#
# Se mantiene intencionadamente amplia para absorber variantes con
# acentos, puntos, plurales y formas mal escritas. Si no hay match
# la unidad se clasifica como "unknown" y el pipeline lo propaga al
# prompt IA para que la IA decida (la IA puede reconocer unidades
# inusuales que no estén en esta tabla).
_UNIT_ALIASES: dict[str, UnitCategory] = {
    # -------- Masa --------
    "kg": "mass", "kgs": "mass", "kilo": "mass", "kilos": "mass",
    "kilogramo": "mass", "kilogramos": "mass",
    "g": "mass", "gr": "mass", "grs": "mass", "gramo": "mass", "gramos": "mass",
    "t": "mass", "tn": "mass", "tm": "mass", "ton": "mass", "tons": "mass",
    "tonelada": "mass", "toneladas": "mass",
    # -------- Volumen --------
    "m3": "volume", "m^3": "volume", "mc": "volume",
    "metrocubico": "volume", "metroscubicos": "volume",
    "l": "volume", "lt": "volume", "lts": "volume",
    "litro": "volume", "litros": "volume",
    "dm3": "volume", "cm3": "volume", "cl": "volume", "ml": "volume",
    # -------- Longitud --------
    "m": "length", "ml": "length",  # ml = metro lineal en construcción
    "metro": "length", "metros": "length",
    "metrolineal": "length", "metroslineales": "length",
    "cm": "length", "mm": "length", "km": "length",
    # -------- Área --------
    "m2": "area", "m^2": "area",
    "metrocuadrado": "area", "metroscuadrados": "area",
    "cm2": "area", "ha": "area", "hectarea": "area", "hectareas": "area",
    # -------- Contable --------
    "ud": "count", "uds": "count", "und": "count", "unds": "count",
    "unidad": "count", "unidades": "count",
    "pza": "count", "pzs": "count", "pieza": "count", "piezas": "count",
    "par": "count", "pares": "count",
    "caja": "count", "cajas": "count",
    "bolsa": "count", "bolsas": "count",
    "saco": "count", "sacos": "count",
    "rollo": "count", "rollos": "count",
    "palet": "count", "pallet": "count", "palets": "count", "pallets": "count",
    # -------- Tiempo --------
    "h": "time", "hr": "time", "hrs": "time",
    "hora": "time", "horas": "time",
    "min": "time", "minuto": "time", "minutos": "time",
    "dia": "time", "dias": "time",
    "jornada": "time", "jornadas": "time",
    # -------- Partida alzada --------
    "pa": "lump_sum",
    "partidaalzada": "lump_sum",
    "partidaszadas": "lump_sum",
    "talzado": "lump_sum",
    "tantoalzado": "lump_sum",
}


def _parse_contexto_linea_json(raw: Optional[str]) -> Optional[ContextoLinea]:
    """Deserializa el JSON persistido en albaran_lines_merge.

    Tolerante a fallos: si el JSON está corrupto o no es un objeto,
    devuelve None y loguea. Nunca lanza (la valoración tiene que poder
    continuar aunque un contexto concreto esté mal).
    """
    if raw is None:
        return None
    stripped = raw.strip() if isinstance(raw, str) else ""
    if not stripped:
        return None
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        logger.warning(
            "[prefilter] contexto_linea_json inválido (JSON): %r", raw[:100],
        )
        return None
    if not isinstance(data, dict):
        logger.warning(
            "[prefilter] contexto_linea_json no es objeto: %r", raw[:100],
        )
        return None
    try:
        return ContextoLinea(**data)
    except Exception as exc:
        logger.warning(
            "[prefilter] contexto_linea_json no casa con ContextoLinea: "
            "raw=%r err=%s",
            raw[:100], exc,
        )
        return None


class UnitCategoryPrefilter:
    """Clasifica la unidad de medida de cada línea en una categoría.

    No hace conversiones: solo asigna categoría. Las conversiones son
    responsabilidad del servicio 6 con su ``UnitRegistry``.

    Adicionalmente, deserializa el ``contexto_linea_json`` crudo que
    llega del repositorio y lo pasa ya tipado al ``AlbaranLineForValuation``
    que acabará en el prompt del LLM.

    Tanda descuento (abr 2026): propaga ``descuento`` y ``precio_neto``
    de cada línea cruda al DTO ``AlbaranLineForValuation``. Estos
    campos viajarán por el envelope hasta el svc6 donde se aplicarán
    en el cálculo del importe valorado.
    """

    def classify(self, unidad: str | None) -> UnitCategory:
        if unidad is None:
            return "unknown"
        token = self._normalize(unidad)
        if not token:
            return "unknown"
        if token in _UNIT_ALIASES:
            return _UNIT_ALIASES[token]
        # Intento con variantes comunes sin el último carácter (plural).
        if token.endswith("s") and token[:-1] in _UNIT_ALIASES:
            return _UNIT_ALIASES[token[:-1]]
        logger.debug("Unidad no clasificada: %r → unknown", unidad)
        return "unknown"

    def build_albaran_lines(
        self,
        raw_lines: Iterable[RawAlbaranLine],
    ) -> list[AlbaranLineForValuation]:
        return [
            AlbaranLineForValuation(
                merge_line_id=line.merge_line_id,
                line_index=line.line_index,
                codigo=line.codigo,
                descripcion=line.descripcion,
                unidad_medida=line.unidad_medida,
                unidad_categoria=self.classify(line.unidad_medida),
                cantidad=line.cantidad,
                precio_unitario_albaran=line.precio_unitario_albaran,
                importe_albaran=line.importe_albaran,
                codigo_partida_albaran=line.codigo_partida_albaran,
                contexto_linea=_parse_contexto_linea_json(
                    line.contexto_linea_json
                ),
                # Tanda descuento — abr 2026
                descuento_albaran=line.descuento,
                precio_neto_albaran=line.precio_neto,
            )
            for line in raw_lines
        ]

    def build_contrato_lines(
        self,
        raw_lines: Iterable[RawContratoLine],
    ) -> list[ContratoLineForValuation]:
        return [
            ContratoLineForValuation(
                contrato_line_id=line.contrato_line_id,
                codigo_contrato=line.codigo_contrato,
                codigo_producto=line.codigo_producto,
                descripcion=line.descripcion,
                unidad_medida=line.unidad_medida,
                unidad_categoria=self.classify(line.unidad_medida),
                precio_unitario=line.precio_unitario,
                codigo_partida=line.codigo_partida,
            )
            for line in raw_lines
        ]

    @staticmethod
    def _normalize(value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value)
        ascii_ = "".join(
            ch for ch in normalized if not unicodedata.combining(ch)
        )
        lowered = ascii_.lower()
        # Quita espacios, puntos, barras, guiones.
        cleaned = re.sub(r"[\s./\\\-]+", "", lowered)
        return cleaned
