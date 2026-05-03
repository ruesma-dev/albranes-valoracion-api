# albaranes-valuation-api (Servicio 5 / sv5)

> **Inferencia IA para valoración de albaranes** del ecosistema Construcciones Ruesma.
> Recibe un `document_id` ya persistido por sv3, carga el contexto desde la BBDD
> compartida (líneas de albarán + líneas de contrato + cabecera), descarga el PDF
> del contrato desde SharePoint, lo manda a un LLM (Claude por defecto) con un
> prompt específico de valoración y devuelve el **envelope** con el matching
> albarán↔contrato y las **líneas sintéticas** de modificadores implícitos.
>
> **No persiste**. Devuelve JSON al llamante (sv6) para que sea él quien lo guarde.

---

## 1. Aclaración importante sobre el nombre

Cuando hablamos de "API de valoración" en este sistema hay **dos** servicios
que aparecen mencionados en el código de sv3:

| Servicio                          | Responsabilidad                                                                                                  |
|-----------------------------------|------------------------------------------------------------------------------------------------------------------|
| **sv5 — `albaranes-valuation-api`** *(este)* | **Inferencia IA**: ¿qué precio del contrato corresponde a cada línea del albarán? ¿Qué modificadores faltan? |
| **sv6 — el orquestador de valoración**       | **Persistencia y orquestación**: `/v1/valuation/run-async`, `/{doc}/re-run`, escribe `albaran_valuations`, etc. |

`sv5` es **stateless** y solo computa. El `sv6` (que aún no he analizado) es quien
expone los endpoints `run-async` / `re-run` que sv3 dispara, llama internamente a sv5
para la inferencia IA, y persiste los resultados en las 9 tablas que sv3 ya creó
en el DDL embebido al arranque.

Esto encaja con el comentario que vi en el código de sv3:
> *"el `/re-run` síncrono incluye la llamada a la IA del **servicio 5** + persistencia"*.

---

## 2. ¿Qué hace exactamente?

`albaranes-valuation-api` toma un albarán ya extraído + un contrato ya enriquecido
(ambos en la BBDD del sv3) y **delega en un LLM** la tarea de:

1. **Casar cada línea del albarán** con la línea del contrato más probable
   (fase 1a sobre la tabla del ERP, fase 1b sobre el PDF del contrato firmado).
2. **Generar líneas sintéticas** que representen modificadores implícitos del
   hormigón que NO aparecen como línea en el albarán pero SÍ están tarifados en
   el PDF del contrato (incremento por año, consistencia, árido, aditivo,
   gestión de residuos, exceso de tiempo de descarga, carga incompleta).

Antes de llamar al LLM aplica un **pre-filtrado determinista**: clasifica
cada `unidad_medida` en una `UnitCategory` (`mass`, `volume`, `length`, `area`,
`count`, `time`, `lump_sum`, `unknown`) y deserializa el `contexto_linea_json`
persistido por sv3. Eso reduce el espacio de candidatos y el LLM se centra en
discriminar dentro de la misma categoría.

---

## 3. Lugar dentro del ecosistema (6 microservicios)

```
                                     ┌──────────────────────────────┐
                                     │ sv6 · valuation orchestrator │
                                     │ (run-async / re-run)         │
                                     └─────┬────────────────────┬───┘
                                           │ POST /v1/albaranes/value
                                           │ {document_id,
                                           │  codigo_contrato?}
                                           ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  sv5 · albaranes-valuation-api          ← ESTE SERVICIO          │
   │  (FastAPI + uvicorn, stateless)                                  │
   │                                                                  │
   │   ┌────────────────────────────────────────────────────────────┐ │
   │   │ Pipeline value_albaran:                                    │ │
   │   │  load_context(BBDD)                                        │ │
   │   │     └─→ sin contrato seleccionado → envelope no_contract   │ │
   │   │  prefilter (categorías de unidad, contexto_linea)          │ │
   │   │  download PDF contrato (best-effort)                       │ │
   │   │  llamada(s) LLM (claude/gemini/openai)                     │ │
   │   │  build envelope (primary + secondaries + context + debug)  │ │
   │   └────────────────────────────────────────────────────────────┘ │
   └────┬──────────────┬───────────────────────────────────────┬──────┘
        │ SELECT       │ download PDF                          │ LLM call
        ▼              ▼                                       ▼
   ┌──────────┐ ┌──────────────────────┐ ┌─────────────────────────────────┐
   │ Postgres │ │ SharePoint (Graph)   │ │ Claude (default) · Gemini · OpenAI│
   │ (sv3 BBDD│ │ (PDF contrato        │ │ (vision API + structured output) │
   │  read-   │ │  vía relative_path   │ │                                  │
   │  only)   │ │  guardado por sv3)   │ │                                  │
   └──────────┘ └──────────────────────┘ └─────────────────────────────────┘

       envelope JSON  ───────►  sv6 (persiste en albaran_valuations + lines)
```

**sv5 es la única "fábrica" de valoraciones IA**. El resto del ecosistema solo
consume su salida (envelope JSON).

---

## 4. Arquitectura interna (Hexagonal / Clean)

```
albaranes-valuation-api/
├─ main.py                                                   # uvicorn.run(build_app(settings))
├─ config/
│  ├─ settings.py                                            # Pydantic-settings + validators (= sv2)
│  ├─ logging_config.py                                      # RotatingFileHandler + consola
│  ├─ prompts.yaml                                           # ⭐ prompt 'valuation_es' V3 (725 líneas)
│  └─ prompts/svc5_prompt_valuation_es.yaml                  # Versión histórica / alternativa
├─ domain/
│  ├─ models/
│  │  ├─ schema_base.py                                      # StrictSchemaModel (extra='forbid')
│  │  ├─ albaran_models.py                                   # Modelo de albarán (subset, NO se usa para output IA)
│  │  ├─ contexto_linea.py                                   # Idéntico al de sv2/sv3 (extra='ignore')
│  │  ├─ llm_attachment.py                                   # LlmAttachment (kind, mime, bytes)
│  │  ├─ valuation_context.py                                # AlbaranLineForValuation, ContratoLineForValuation, ContextoValoracion
│  │  └─ valuation_models.py                                 # ⭐ LineValuation V3 + DocumentoValoracion (output IA)
│  └─ ports/
│     ├─ llm_client.py                                       # LlmVisionClient (attachment OPCIONAL)
│     ├─ prompt_repository.py
│     ├─ valuation_context_repository.py                     # load_context() + RawAlbaranLine, RawContratoLine, RawContratoHeader
│     └─ contrato_pdf_downloader.py                          # download_by_relative_path()
├─ application/
│  ├─ pipelines/
│  │  └─ value_albaran_pipeline.py                           # ⭐ Pipeline POST /value
│  └─ services/
│     ├─ unit_category_prefilter.py                          # Clasifica unidad → UnitCategory + parsea contexto_linea
│     ├─ valuation_extraction_service.py                     # Orquesta llamadas a N proveedores LLM
│     └─ schema_registry.py                                  # Mapa nombre_schema → DocumentoValoracion
├─ infrastructure/
│  ├─ database/
│  │  ├─ session_factory.py                                  # SQLAlchemy engine (read-only)
│  │  └─ sqlalchemy_valuation_context_repository.py          # SQL crudo (text()) sobre tablas merge del sv3
│  ├─ graph/
│  │  └─ token_provider.py                                   # OAuth2 client_credentials (= sv1/sv3)
│  ├─ storage/
│  │  └─ sharepoint_contrato_pdf_downloader.py               # Descarga PDF contrato via Graph (3 modos)
│  ├─ llm/
│  │  ├─ retry_policy.py                                     # Backoff exponencial + jitter (= sv2)
│  │  ├─ openai_responses_client.py                          # responses.parse(text_format=Pydantic)
│  │  ├─ openai_sdk_compat.py                                # Monkey-patch OpenAI/Pydantic v2 (= sv2)
│  │  ├─ gemini_genai_client.py                              # generate_content(response_json_schema)
│  │  ├─ claude_messages_client.py                           # messages.create + tool 'emit_valuation_result'
│  │  └─ llm_call_logger.py                                  # Logger defensivo de request/response a disco
│  └─ prompts/
│     └─ yaml_prompt_repository.py                           # Carga prompts.yaml a memoria
└─ interface_adapters/
   └─ api/
      └─ app.py                                              # FastAPI: build_app() + /health + /v1/albaranes/value
```

### Patrones aplicados

| Patrón                                       | Dónde                                                          | Por qué                                                                                          |
|----------------------------------------------|----------------------------------------------------------------|--------------------------------------------------------------------------------------------------|
| **Hexagonal / Ports & Adapters**             | `domain/ports` ↔ `infrastructure/*`                            | 4 puertos limpios: LLM, prompts, BBDD context, PDF downloader.                                   |
| **Pipeline**                                 | `ValueAlbaranPipeline.run`                                     | Pasos lineales con responsabilidad única.                                                        |
| **Strategy + Factory condicional**           | `build_app()` + 3 `LlmVisionClient`                            | Cada proveedor habilitado por flag, mismo contrato.                                              |
| **Repository**                               | `SqlAlchemyValuationContextRepository`                         | SQL crudo `text()` aislado de la lógica de negocio.                                              |
| **Pre-filtrado determinista**                | `UnitCategoryPrefilter`                                        | Reduce ambigüedad antes de IA: clasifica unidades con tabla amplia + plurales.                   |
| **Defensa en profundidad (Pydantic)**        | `LineValuation._backfill_razon_corta_for_synthetic`            | Si el LLM olvida `razon_corta` en sintéticas, fallback a `modifier_reason` o `descripcion_linea`. |
| **Validación cruzada (Pydantic `model_validator`)** | `LineValuation._validate_kind_coherence`              | Garantiza coherencia: `from_albaran`/`synthetic_modifier` con campos compatibles.                |
| **Retry exponencial con jitter**             | `RetryPolicy` + `run_with_retry`                               | Idéntico al de sv2: 4 mecanismos de detección retryable.                                         |
| **Composition root**                         | `build_app(settings)`                                          | Único sitio donde se cablean dependencias.                                                       |
| **Best-effort downstream**                   | Descarga PDF: si falla, sigue sin PDF; si > MAX_PDF_MB, idem.  | La valoración degrada elegantemente a "solo fase 1a" en lugar de fallar.                         |

---

## 5. Endpoints HTTP

### 5.1 `GET /health`

```json
{
  "ok": true,
  "service": "albaranes-valuation-api",
  "version": "1.0.0",
  "enabled_providers": ["claude"],
  "prompt_key": "valuation_es"
}
```

> Por defecto **solo Claude está habilitado** (decisión del cliente). Las flags
> de OpenAI/Gemini existen para activarlos sin tocar el código.

### 5.2 `POST /v1/albaranes/value`

Endpoint único de inferencia.

**Request:**

```http
POST /v1/albaranes/value HTTP/1.1
Content-Type: application/json

{
  "document_id": "8d9a2f66-...-a3e1",
  "codigo_contrato": null
}
```

| Campo              | Tipo               | Default | Descripción                                                                     |
|--------------------|--------------------|---------|---------------------------------------------------------------------------------|
| `document_id`      | `string` *obligatorio* | —       | UUID del documento en `albaran_documents_merge` (lo asignó sv3).                |
| `codigo_contrato`  | `string \| null`   | `null`  | Si se pasa, **ignora** el `selected_contrato_codigo` y usa este. Útil cuando el revisor quiere re-valorar contra otro contrato sin cambiar la selección. |

**Errores:**

| Código | Causa                                                                                  |
|--------|----------------------------------------------------------------------------------------|
| `404`  | Repositorio no encuentra el documento (`KeyError`).                                    |
| `400`  | Documento sin líneas de albarán → no se puede valorar (`ValueError`).                  |
| `500`  | Error inesperado (LLM caído tras retries, BBDD caída, fallo de validación Pydantic).   |

**Respuesta `200 OK` (caso "ok"):**

```json
{
  "status": "ok",
  "meta": {
    "document_id": "8d9a2f66-...-a3e1",
    "codigo_contrato": "C-2026-014",
    "pdf_relative_path": "albaranes/2026/05/contratos/C-2026-014_123_foo.pdf",
    "pdf_filename": "C-2026-014_123_foo.pdf",
    "pdf_sha256": "f3e1...",
    "prompt_key": "valuation_es",
    "schema": "documento_valoracion",
    "primary_provider": "claude",
    "model": "claude-sonnet-4-5",
    "processed_at_utc": "2026-05-03T12:34:56.789012+00:00",
    "service": "albaranes-valuation-api",
    "service_version": "1.0.0",
    "providers_used": ["claude"]
  },
  "data": {
    "lineas": [
      {
        "merge_line_id": 4521,
        "line_kind": "from_albaran",
        "match_method": "exact_concept",
        "matched_contrato_line_id": 1187,
        "match_confidence_pct": 96.0,
        "unidad_categoria_albaran": "volume",
        "unidad_category_match": true,
        "precio_unitario_contrato_db": 87.5,
        "precio_unitario_pdf_inferido": 87.5,
        "razon_corta": "HM-25 base coincide exactamente con línea 1187 del contrato"
      },
      {
        "merge_line_id": null,
        "line_kind": "synthetic_modifier",
        "parent_merge_line_id": 4521,
        "modifier_source": "tiempo_exceso",
        "modifier_reason": "vehículo retrasado 33 min sobre límite de 45 min",
        "descripcion_linea": "INCREMENTO POR EXCESO DE TIEMPO DE DESCARGA",
        "cantidad_override": 33,
        "rol_linea": "incremento_tiempo",
        "match_method": "no_match",
        "match_confidence_pct": 85.0,
        "unidad_categoria_albaran": "time",
        "unidad_category_match": false,
        "precio_unitario_contrato_db": null,
        "precio_unitario_pdf_inferido": 0.5,
        "pdf_inference_reasoning": "Tabla 'Recargos por exceso de tiempo' del PDF, página 3, cláusula 4.2",
        "razon_corta": "vehículo retrasado 33 min sobre límite de 45 min"
      }
    ]
  },
  "context": {
    "lineas_albaran": [/* AlbaranLineForValuation serializadas */],
    "lineas_contrato": [/* ContratoLineForValuation serializadas */]
  },
  "debug": {
    "claude": {
      "claude_request":  { "...": "..." },
      "claude_response": { "...": "..." }
    }
  }
}
```

**Respuesta `200 OK` (caso "no_contract"):** si el documento no tiene
`selected_contrato_codigo` o el override apunta a uno inexistente, sv5 **NO**
llama a IA y devuelve un envelope marcado para que sv6 lo persista como tal:

```json
{
  "status": "no_contract",
  "meta": {
    "document_id": "8d9a2f66-...",
    "codigo_contrato": null,
    "processed_at_utc": "...",
    "service": "albaranes-valuation-api",
    "service_version": "1.0.0",
    "prompt_key": null,
    "providers_used": []
  },
  "data": {"lineas": []},
  "context": {
    "lineas_albaran": [/* RawAlbaranLine sin clasificar */],
    "lineas_contrato": []
  },
  "debug": {}
}
```

---

## 6. Schema de salida (`DocumentoValoracion` / `LineValuation` V3)

Pydantic estricto (`extra='forbid'`). Campos por línea:

### 6.1 Identidad de la línea

| Campo                         | Tipo                                       | from_albaran     | synthetic_modifier |
|-------------------------------|--------------------------------------------|------------------|---------------------|
| `merge_line_id`               | `int \| None`                              | **obligatorio**  | `null`              |
| `line_kind`                   | `'from_albaran' \| 'synthetic_modifier'`   | `'from_albaran'` | `'synthetic_modifier'` |
| `parent_merge_line_id`        | `int \| None`                              | `null`           | **obligatorio**     |
| `descripcion_linea`           | `str \| None`                              | `null`           | **obligatorio**     |

### 6.2 Solo en sintéticas (`synthetic_modifier`)

| Campo               | Tipo / Literal                                                                                     |
|---------------------|----------------------------------------------------------------------------------------------------|
| `modifier_source`   | `'codigo_producto' \| 'observaciones' \| 'year_contract' \| 'year_albaran' \| 'tiempo_exceso' \| 'gestion_residuos' \| 'carga_incompleta' \| 'otro'` |
| `modifier_reason`   | `str \| None` — razón corta legible.                                                                |
| `cantidad_override` | `float \| None` — solo si la cantidad la calcula el LLM (`tiempo_exceso` en min, `carga_incompleta` en m³). |
| `rol_linea`         | `'incremento_year' \| 'incremento_consistencia' \| 'incremento_arido' \| 'incremento_aditivo' \| 'incremento_residuos' \| 'incremento_tiempo' \| 'incremento_carga_incompleta' \| 'incremento_otro'` |

### 6.3 Resultado del matching (común)

| Campo                            | Tipo                                          |
|----------------------------------|-----------------------------------------------|
| `match_method`                   | `'exact_concept' \| 'semantic' \| 'price_only' \| 'no_match'` |
| `matched_contrato_line_id`       | `int \| None`                                 |
| `match_confidence_pct`           | `float` `[0, 100]`                            |
| `unidad_categoria_albaran`       | `str` (`mass \| volume \| length \| area \| count \| time \| lump_sum \| unknown`) |
| `unidad_category_match`          | `bool`                                        |
| `precio_unitario_contrato_db`    | `float \| None` — fase 1a (línea de tabla).   |
| `precio_unitario_pdf_inferido`   | `float \| None` — fase 1b (PDF).              |
| `pdf_inference_reasoning`        | `str \| None`                                 |
| `razon_corta`                    | `str` — auditoría (con fallback defensivo).   |

### 6.4 Validaciones cruzadas en el modelo (`@model_validator`)

**`mode='before'`** — *backfill de `razon_corta` para sintéticas:* el prompt V3
inicialmente listaba mal los campos obligatorios y el LLM omitía `razon_corta` en
sintéticas. Como hacer el campo `Optional` degradaría la auditoría de las líneas
reales, se intercepta antes de la validación y SOLO en sintéticas se rellena con
`modifier_reason` o `descripcion_linea`. Las `from_albaran` siguen rompiendo si
no traen `razon_corta` (es bug real del LLM).

**`mode='after'`** — *coherencia de `line_kind`*:
- `from_albaran` ⇒ `merge_line_id` no nulo, `parent_merge_line_id` nulo.
- `synthetic_modifier` ⇒ `merge_line_id` nulo, `parent_merge_line_id` no nulo, `descripcion_linea` no nula.

> **Lo que el LLM rellena**: matching (fases 1a y 1b) + categoría de unidad +
> confianza + razón. **Lo que NO rellena**: partida final, cantidad convertida,
> importe calculado. Eso es responsabilidad del **sv6** (con su `UnitRegistry` y
> la fórmula `importe = cantidad × precio_contrato × (1 - descuento/100)`).

---

## 7. Pre-filtrado determinista (`UnitCategoryPrefilter`)

Ejecuta dos transformaciones antes de la llamada IA:

### 7.1 Clasificación de `unidad_medida` → `UnitCategory`

8 categorías canónicas: `mass`, `volume`, `length`, `area`, `count`, `time`,
`lump_sum`, `unknown`. La tabla de aliases es muy amplia para tolerar variantes:

| Categoría    | Aliases reconocidos (extracto)                                                                                |
|--------------|---------------------------------------------------------------------------------------------------------------|
| `mass`       | `kg`, `kgs`, `kilo`, `kilos`, `kilogramo`, `kilogramos`, `g`, `gr`, `t`, `tn`, `tm`, `ton`, `tonelada`, …      |
| `volume`     | `m3`, `m^3`, `mc`, `metrocubico`, `metroscubicos`, `l`, `lt`, `lts`, `litro`, `litros`, `dm3`, `cm3`, `cl`, `ml` |
| `length`     | `m`, `ml` *(metro lineal en construcción)*, `metro`, `metros`, `cm`, `mm`, `km`, `metrolineal`, …             |
| `area`       | `m2`, `m^2`, `metrocuadrado`, `metroscuadrados`, `cm2`, `ha`, `hectarea`, …                                   |
| `count`      | `ud`, `uds`, `und`, `pza`, `pzs`, `pieza`, `caja`, `bolsa`, `saco`, `palet`, `pallet`, …                      |
| `time`       | `h`, `hr`, `hrs`, `hora`, `horas`, `min`, `minuto`, `dia`, `dias`, `jornada`, `jornadas`                      |
| `lump_sum`   | `pa`, `partidaalzada`, `talzado`, `tantoalzado`, …                                                            |

Normalización antes del lookup: NFKD + minúsculas + remoción de espacios/puntos/barras/guiones.
Si no hay match exacto, prueba con el token sin "s" (plurales). Si nada cuadra → `unknown`
y se delega a la IA (puede reconocer unidades raras).

> **Atención**: `ml` es ambiguo. La tabla lo mapea a `length` (metro lineal,
> mucho más frecuente en construcción). Si en algún proveedor aparece como
> `ml` = mililitros, el LLM puede corregirlo en `unidad_categoria_albaran`,
> pero la categoría inicial será `length`.

### 7.2 Deserialización de `contexto_linea_json`

El JSON crudo de `albaran_lines_merge.contexto_linea_json` (lo que persistió
sv3) se deserializa a `ContextoLinea` Pydantic. **Tolerante**: si el JSON está
corrupto, no es objeto, o no casa con el modelo, devuelve `None` con un
`logger.warning` y la valoración continúa.

---

## 8. Configuración (variables de entorno)

### 8.1 Generales del servicio

| Variable          | Default     | Descripción                                                  |
|-------------------|-------------|--------------------------------------------------------------|
| `API_HOST`        | `127.0.0.1` |                                                              |
| `API_PORT`        | `8002`      | (sv2: 8000, sv3: 8001, sv5: 8002)                            |
| `MAX_PDF_MB`      | `40`        | Tope del PDF de contrato. Si lo supera → se omite y solo fase 1a. |
| `HTTP_TIMEOUT_S`  | `60`        | Timeout HTTP de Graph y otros.                               |
| `LOG_LEVEL`       | `INFO`      |                                                              |
| `LOG_DIR`         | `logs`      |                                                              |
| `SERVICE_VERSION` | `1.0.0`     | Aparece en `/health` y `meta.service_version` del envelope.  |
| `PROMPT_KEY`      | `valuation_es` |                                                            |
| `PROMPTS_YAML_PATH` | `config/prompts.yaml` |                                                  |

### 8.2 Proveedores LLM

| Variable                | Default              | Notas                                                       |
|-------------------------|----------------------|-------------------------------------------------------------|
| `ENABLE_OPENAI`         | **`false`**          | Por defecto **off** (a diferencia de sv2).                  |
| `ENABLE_GEMINI`         | **`false`**          | Por defecto **off**.                                        |
| `ENABLE_CLAUDE`         | **`true`**           | Único habilitado por defecto (decisión del cliente).        |
| `OPENAI_API_KEY`        | *(opc. si off)*      |                                                             |
| `OPENAI_MODEL`          | `gpt-5`              |                                                             |
| `GEMINI_API_KEY`        | *(opc. si off)*      |                                                             |
| `GEMINI_MODEL`          | `gemini-2.5-flash`   |                                                             |
| `ANTHROPIC_API_KEY`     | *obl. si Claude on*  |                                                             |
| `ANTHROPIC_MODEL`       | `claude-sonnet-4-5`  |                                                             |
| `ANTHROPIC_MAX_TOKENS`  | `8192`               |                                                             |
| `ANTHROPIC_TIMEOUT_S`   | `180`                | **Más alto que sv2 (120 s)** porque la valoración es más larga. |

### 8.3 Política de reintentos

| Variable               | Default | Descripción                                                                |
|------------------------|---------|----------------------------------------------------------------------------|
| `LLM_MAX_RETRIES`      | `2`     | Reintentos adicionales (total = 1 + retries).                              |
| `LLM_BACKOFF_BASE_S`   | `2.0`   |                                                                            |
| `LLM_BACKOFF_CAP_S`    | `30.0`  |                                                                            |

### 8.4 PostgreSQL (read-only, BBDD compartida con sv3 y sv6)

| Variable          | Default     |
|-------------------|-------------|
| `PG_HOST`         | `localhost` |
| `PG_PORT`         | `5432`      |
| `PG_DB`           | `albaranes` |
| `PG_USER`         | *obl.*      |
| `PG_PASSWORD`     | *obl.*      |

> **Solo lectura.** sv5 nunca hace `INSERT`/`UPDATE`. No ejecuta DDL. No crea la
> BBDD. Si arranca antes que sv3 y la BBDD no existe, falla en el primer SELECT —
> es responsabilidad operativa coordinar el orden de arranque, o aceptar el fallo
> hasta que sv3 lo cree (sv5 NO crashea, devolverá 500 al primer `/value`).

### 8.5 SharePoint (descarga PDF de contrato)

| Variable                    | Default | Notas                                                |
|-----------------------------|---------|------------------------------------------------------|
| `GRAPH_KEY`                 | *obl.*  | JSON o JSON-base64 con `tenant_id`/`client_id`/`client_secret`. |
| `SHAREPOINT_MODE`           | `drive_id` | `drive_id` \| `folder_url` \| `site_path`         |
| `SHAREPOINT_DRIVE_ID`       | *si modo drive_id* |                                            |
| `SHAREPOINT_FOLDER_URL`     | *si modo folder_url* |                                          |
| `SHAREPOINT_HOSTNAME`       | *si modo site_path* |                                            |
| `SHAREPOINT_SITE_PATH`      | *si modo site_path* |                                            |
| `SHAREPOINT_DRIVE_NAME`     | `Documentos compartidos` | (solo modo `site_path`)                |

> sv5 reutiliza el mismo `GraphTokenProvider` que sv1 y sv3. **No usa**
> `SHAREPOINT_FOLDER_ROOT`, `SHAREPOINT_LINK_TYPE`, etc.: solo descarga, no sube.

### 8.6 Validators cruzados

- *Al menos un proveedor LLM debe estar habilitado* (los 3 a `false` aborta el boot).
- *Si un proveedor está habilitado, su API key debe existir.*

### 8.7 Ejemplo de `.env`

```dotenv
# --- Generales ---
API_HOST=0.0.0.0
API_PORT=8002
MAX_PDF_MB=40
LOG_LEVEL=INFO
LOG_DIR=logs
SERVICE_VERSION=1.0.0
PROMPT_KEY=valuation_es

# --- LLMs (solo Claude por defecto) ---
ENABLE_OPENAI=false
ENABLE_GEMINI=false
ENABLE_CLAUDE=true
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-5
ANTHROPIC_MAX_TOKENS=8192
ANTHROPIC_TIMEOUT_S=180

# --- Retry compartida ---
LLM_MAX_RETRIES=2
LLM_BACKOFF_BASE_S=2.0
LLM_BACKOFF_CAP_S=30.0

# --- BBDD (compartida con sv3, RO) ---
PG_HOST=localhost
PG_PORT=5432
PG_DB=albaranes
PG_USER=albaranes_ro
PG_PASSWORD=********

# --- SharePoint (solo descarga) ---
GRAPH_KEY={"tenant_id":"...","client_id":"...","client_secret":"..."}
SHAREPOINT_MODE=drive_id
SHAREPOINT_DRIVE_ID=b!...
```

---

## 9. Flujo detallado del pipeline `/value`

```
1. POST /v1/albaranes/value {document_id, codigo_contrato?}

2. ValueAlbaranPipeline.run():
   a) raw_ctx = context_repository.load_context(doc_id, override)
        ├─ SELECT albaran_documents_merge WHERE id = :doc_id
        ├─ SELECT albaran_lines_merge      WHERE document_id = :doc_id  (líneas albarán)
        ├─ SELECT albaran_contratos_merge  WHERE document_id = :doc_id AND codigo_contrato = :codigo
        │      (codigo = override si se pasa, si no, selected_contrato_codigo del header)
        └─ SELECT albaran_contrato_lines_merge WHERE codigo_contrato = :codigo (líneas contrato)

   b) if not raw_ctx.lineas_albaran: raise ValueError(...)        # → 400

   c) if raw_ctx.contrato is None:
         return envelope(status="no_contract", lineas=[], debug={})  # NO IA

   d) lineas_albaran = prefilter.build_albaran_lines(raw_ctx.lineas_albaran)
        ├─ classify(unidad_medida) → UnitCategory
        ├─ parse(contexto_linea_json) → ContextoLinea (tolerante)
        └─ propaga descuento, precio_neto al DTO

   e) lineas_contrato = prefilter.build_contrato_lines(raw_ctx.lineas_contrato)
        └─ classify(unidad_medida) → UnitCategory

   f) context = ContextoValoracion(...)

   g) pdf_attachment = _try_download_pdf(contrato_header.pdf_relative_path)
        ├─ relative_path None → log info, attachment=None
        ├─ Excepción Graph  → log exception, attachment=None
        └─ Tamaño > MAX_PDF_MB → log warning, attachment=None

   h) results = extraction_service.extract(context, pdf_attachment)
        ├─ Si pdf_attachment is None: user_text += _NOTA_SIN_PDF
        │     (instrucciones explícitas: solo fase 1a, sin sintéticas, etc.)
        └─ Para cada proveedor habilitado:
             llamada SDK con system + user_text + (attachment opcional) + schema
             → parsed: DocumentoValoracion (validado Pydantic con cross-validators)

   i) build_envelope(context, results, pdf_attachment)
        ├─ primary = primero existente en orden: claude > gemini > openai
        ├─ envelope.data = primary.parsed.model_dump()
        ├─ envelope.debug = {primary_name: primary.debug_payload}
        └─ Para cada secundario: envelope[name] = {meta, data, debug}

   j) return envelope (200 OK)
```

### Manejo del caso "sin PDF"

Antes de la "tanda PDF opcional" (abr/may 2026), el servicio inventaba un PDF dummy
de 15 bytes cuando el contrato no tenía PDF. Eso funcionaba con Anthropic y OpenAI
pero **Gemini lo rechazaba** con `400 INVALID_ARGUMENT: The document has no pages.`

Solución actual (visible en el código y comentada en detalle):
- `LlmAttachment` es **opcional** en el puerto.
- Los 3 clientes LLM saben construir bloques solo-texto sin documento.
- `valuation_extraction_service` añade al `user_text` el bloque `_NOTA_SIN_PDF`
  con instrucciones explícitas para el LLM:
  - Solo fase 1a (matching contra `lineas_contrato` del contexto).
  - `precio_unitario_pdf_inferido = null` en todas las líneas.
  - `pdf_inference_reasoning = null`.
  - **NO emitir líneas sintéticas** (sin PDF no se pueden inferir tarifas).
  - Líneas no encontradas → `match_method='no_match'`.

---

## 10. Cómo se invoca este servicio

### 10.1 Quién lo llama hoy

- **`sv6` (orquestador de valoración)** lo llamará desde:
  - `/v1/valuation/run-async` (BG worker tras el 202 a sv3) → fire-and-forget interno.
  - `/v1/valuation/{doc}/re-run` (síncrono, lanzado por el front).
- **El frontend humano (sv4)** podría llamarlo directamente para previews,
  pero el flujo canónico es vía sv6.

### 10.2 Curl de prueba

```bash
curl -X POST http://127.0.0.1:8002/v1/albaranes/value \
  -H "Content-Type: application/json" \
  -d '{"document_id": "8d9a2f66-0b6e-4d3a-a3e1-...", "codigo_contrato": null}'
```

```python
import httpx

resp = httpx.post(
    "http://127.0.0.1:8002/v1/albaranes/value",
    json={"document_id": "8d9a2f66-...", "codigo_contrato": None},
    timeout=300,
)
resp.raise_for_status()
envelope = resp.json()
```

### 10.3 Arranque local

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env       # rellenar GRAPH_KEY, ANTHROPIC_API_KEY, PG_*, etc.
python main.py               # uvicorn en API_HOST:API_PORT (default 8002)
```

> El servicio asume que la BBDD ya existe y tiene las tablas merge del sv3.
> No ejecuta DDL ni crea esquemas.

### 10.4 Decisión de despliegue Azure

Mismo perfil que sv2/sv3 — recomendación: **Azure Container App** en el spoke DEV:

- `minReplicas=1` para evitar cold start de los SDKs LLM (Anthropic y `google-genai`
  pesan al cargar).
- `maxReplicas` 3-5 (cada petición consume CPU/red en la inferencia LLM y memoria
  en el PDF descargado).
- *Ingress interno*: el llamante natural es sv6, que vive en el mismo spoke.
- Identidad gestionada para Key Vault references (`ANTHROPIC_API_KEY`, `GRAPH_KEY`,
  `PG_PASSWORD`).

> **No** usar Azure Functions: las peticiones pueden superar 90-180 s con Claude
> + PDFs grandes; el `ANTHROPIC_TIMEOUT_S=180` ya es indicativo. Functions HTTP
> Trigger en consumo tope a 230 s, lo justo, pero el cold start de los SDKs no
> compensa.

---

## 11. Inputs / Outputs del servicio

### Inputs

| Origen      | Naturaleza                      | Detalle                                                                              |
|-------------|---------------------------------|--------------------------------------------------------------------------------------|
| HTTP        | `POST /v1/albaranes/value`      | JSON `{document_id, codigo_contrato?}`                                               |
| `.env`      | Configuración estática          | LLM keys + Postgres + Graph + flags + límites.                                       |
| PostgreSQL  | SELECTs sobre tablas merge sv3  | Cabecera, líneas albarán, cabecera contrato, líneas contrato.                        |
| SharePoint  | GET binario (Graph)             | PDF del contrato firmado, vía `pdf_relative_path` que sv3 dejó al subirlo.           |
| LLM APIs    | Respuestas estructuradas        | Claude (default) / Gemini / OpenAI con tool/JSON schema para `DocumentoValoracion`.  |

### Outputs

| Destino    | Naturaleza            | Detalle                                                                                        |
|------------|-----------------------|------------------------------------------------------------------------------------------------|
| HTTP       | `application/json`    | Envelope con `status`, `meta`, `data`, `context`, `debug` + sub-objetos por proveedor secundario. |
| Filesystem | Logs rotados          | `logs/...` (config en `logging_config.py`).                                                    |
| Filesystem | (opcional) `LlmCallLogger` | Si está activado, request/response de cada LLM call serializado a JSON en disco para auditoría. |

> **No persiste en BBDD ni en SharePoint**. La persistencia del envelope la hace sv6.

---

## 12. Decisiones técnicas relevantes

1. **Stateless sobre BBDD compartida.** sv5 no posee schema propio: lee las tablas
   merge que escribe sv3. Esto significa que un cambio de schema en sv3 puede
   romper sv5 sin warning. Lo señalo como mejora (§13).
2. **SQL crudo en lugar de ORM.** Decisión explícita del autor: NO importar los
   `orm_models` de sv3 para no acoplar microservicios. La consecuencia es la
   antedicha — el contrato es implícito (nombres de columnas en `text()`) y hay
   que mantenerlo a mano.
3. **Pre-filtrado determinista antes de IA.** La clasificación de unidades es
   barata y reproducible; mandárselo ya hecho al LLM ahorra tokens y reduce
   alucinación. Si el LLM ve `unidad_categoria='volume'` para todas las líneas
   de hormigón, sabe que solo debe matchear contra líneas de contrato con
   `volume`.
4. **Líneas sintéticas como concepto de primera clase** (V3 del prompt). El LLM
   no solo matchea: **inventa líneas** que faltan en el albarán pero existen en
   el contrato (M1-M7). Esto es lo que diferencia sv5 de un matcher trivial. El
   schema Pydantic lleva validators cruzados para garantizar coherencia
   `from_albaran`/`synthetic_modifier`.
5. **Defensa en profundidad en el modelo.** Aunque el prompt YAML pida
   `razon_corta` correctamente, el `model_validator(mode='before')` rellena
   `razon_corta` para sintéticas si el LLM la omite. **Doble red**: si el prompt
   se desincroniza con el código, el sistema sigue funcionando.
6. **PDF opcional con degradación elegante.** Si no hay PDF (no `pdf_relative_path`,
   o falla la descarga, o supera `MAX_PDF_MB`), la valoración se reduce a fase 1a
   (matching contra la tabla del contrato del ERP). El prompt incluye
   instrucciones explícitas (`_NOTA_SIN_PDF`) para este caso.
7. **Override de `codigo_contrato` en el request.** El revisor humano puede pedir
   re-valorar contra OTRO contrato sin tener que llamar antes al PATCH del sv3.
   Es solo para preview: la selección persistente sigue siendo la del sv3.
8. **El proveedor primario es hardcoded en orden `claude > gemini > openai`.** Si
   solo está Claude (default), Claude manda. Si activas los otros, el primary se
   queda en Claude y los demás se anexan como secundarios. Cambiar este orden
   requiere editar código (no hay flag).
9. **`ANTHROPIC_TIMEOUT_S=180`** es alto deliberadamente: la inferencia con PDF
   adjunto + ~30 líneas de contrato puede tardar 60-120 s con Claude Sonnet 4.5.
10. **`MAX_PDF_MB=40`** es generoso: los PDFs de contratos firmados con anexos
    suelen estar en 1-10 MB. El tope evita degradar la inferencia con PDFs
    descomunales (escaneados a alta resolución) que satura el contexto.

---

## 13. Limitaciones conocidas y mejoras propuestas

| #  | Limitación                                                                                                | Mejora propuesta                                                                                                  |
|----|-----------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------|
| 1  | SQL `text()` acopla schemas entre microservicios sin contrato formal                                      | Crear una **VIEW** en PostgreSQL (`v_valuation_context_*`) gestionada por sv3, y hacer SELECT contra ellas en sv5. |
| 2  | Sin auth en endpoints                                                                                     | API key header o Easy Auth (Entra ID) en Container App.                                                           |
| 3  | Llamadas LLM secuenciales (igual problema que sv2)                                                        | `ThreadPoolExecutor` para paralelizar cuando haya >1 proveedor habilitado.                                        |
| 4  | Sin caching de PDF (si el front llama 2 veces seguidas con override → 2 descargas)                        | Cache LRU en memoria por (drive_id, relative_path, sha256) con TTL corto.                                         |
| 5  | Si todos los LLMs fallan tras retries → 500 sin fallback                                                  | Si Claude falla y Gemini está habilitado, intentar Gemini (degradación entre proveedores).                        |
| 6  | Tabla de aliases de unidades estática y monolítica                                                        | Mover a `config/unit_aliases.yaml` para no requerir despliegue para añadir aliases.                              |
| 7  | El proveedor primario es hardcoded                                                                        | Variable `PRIMARY_PROVIDER_ORDER=claude,gemini,openai`.                                                           |
| 8  | El envelope `no_contract` reutiliza `RawAlbaranLine` sin clasificar (no pasa por prefilter)               | Pasar el prefilter siempre, da igual si hay contrato o no — uniformidad en el envelope.                          |
| 9  | El `LlmCallLogger` existe pero no se ve cableado en `app.py`                                              | Cablearlo en `build_app()` con flag `LLM_CALL_LOG_DIR` para activar la auditoría on-demand.                      |
| 10 | El validator `_backfill_razon_corta` parchea un bug del prompt — riesgo de ocultar regresiones            | Loggear cuando se aplica el backfill para detectar si vuelve a dispararse en producción.                          |

---

## 14. Frontera del microservicio

A diferencia del sv3 (donde sí veía cosas que se salían), **sv5 está bien
delimitado**. Su contrato es claro: "dado un `document_id` ya persistido, devuelve
la valoración por IA". No persiste, no toca el ciclo de vida de los contratos, no
dispara nada downstream.

La única advertencia es la dependencia implícita del schema de sv3 (vía SQL crudo),
pero eso es un trade-off común para evitar acoplar paquetes Python entre
microservicios. La mejora #1 (VIEWs gestionadas) es la solución limpia.

---

## 15. Resumen de un vistazo

| Característica         | Valor                                                                                |
|------------------------|--------------------------------------------------------------------------------------|
| Tipo                   | API HTTP (FastAPI + uvicorn)                                                         |
| Lenguaje               | Python 3.12                                                                          |
| Entrada                | `POST /v1/albaranes/value` con `{document_id, codigo_contrato?}`                     |
| Salida                 | Envelope JSON con valoración LLM (matching + sintéticas) + contexto + debug          |
| Persistencia propia    | Ninguna (solo logs). Lee de la BBDD del sv3 en read-only.                            |
| Storage                | SharePoint para descargar PDF de contrato (que sv3 subió).                           |
| LLMs                   | Claude (default), Gemini, OpenAI — flags individuales                                |
| Concurrencia           | Una request por worker uvicorn; proveedores secuenciales por request                 |
| Despliegue objetivo    | Azure Container App (interno al spoke)                                               |
| Punto de entrada       | `python main.py`                                                                     |
| Dependencias clave     | `fastapi`, `uvicorn`, `sqlalchemy` (RO), `psycopg`, `httpx`, `anthropic`, `google-genai`, `openai`, `pydantic-settings`, `PyYAML` |
| Servicios upstream     | sv6 (orquestador valoración) — y sv4 (frontend) si llama directo                     |
| Servicios downstream   | PostgreSQL · SharePoint/Graph · Anthropic / Google / OpenAI                          |

---

*Documento generado a partir del análisis del código del paquete `sv5.zip` aportado.*
