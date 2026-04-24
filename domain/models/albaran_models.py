# domain/models/albaran_models.py
from __future__ import annotations

from typing import List, Optional

from pydantic import Field

from domain.models.contexto_linea import ContextoLinea
from domain.models.schema_base import StrictSchemaModel


class CabeceraAlbaran(StrictSchemaModel):
    proveedor_nombre: Optional[str] = None
    proveedor_cif: Optional[str] = None
    fecha: Optional[str] = None
    numero_albaran: Optional[str] = None
    forma_pago: Optional[str] = None
    obra_codigo: Optional[str] = None
    obra_nombre: Optional[str] = None
    obra_direccion: Optional[str] = None
    id: Optional[str] = None


class LineaAlbaran(StrictSchemaModel):
    id: Optional[str] = None
    cabecera_id: Optional[str] = None
    codigo: Optional[str] = None
    cantidad: Optional[float] = None
    concepto: Optional[str] = None
    # -----------------------------------------------------------------
    # Unidad de medida tal y como aparece en el albarán ('m3', 'kg',
    # 'ud', 'min', 'h'...). Antes estaba embebida implícitamente en
    # ``concepto``; ahora se persiste como campo propio para que el
    # valorador (svc5) y el conversor de unidades (svc6) tengan un
    # dato fiable sin necesidad de re-parsear el concepto.
    #
    # El prompt V2 del OCR ya pide este dato explícitamente.
    # -----------------------------------------------------------------
    unidad_medida: Optional[str] = None
    precio: Optional[float] = None
    descuento: Optional[float] = None
    precio_neto: Optional[float] = None
    codigo_imputacion: Optional[str] = None
    confianza_pct: Optional[float] = Field(default=None, ge=0, le=100)

    # -----------------------------------------------------------------
    # Bloque opcional con info estructural de la línea (familia
    # hormigón / combustible / alquiler_maquinaria / otro). Si la línea
    # no pertenece a una familia compleja, el OCR omite el bloque y
    # llega como None. Ver domain/models/contexto_linea.py y los
    # prompts V2 para la semántica exacta.
    # -----------------------------------------------------------------
    contexto_linea: Optional[ContextoLinea] = None


class DocumentoAlbaran(StrictSchemaModel):
    cabecera: CabeceraAlbaran
    lineas: List[LineaAlbaran]
