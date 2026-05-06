# infrastructure/prompts/revision_rules_repository.py
"""Repositorio de reglas de revisión para fase 2.

Carga ``config/revision_rules.yaml`` al arrancar y expone:

  - ``count``: número de reglas cargadas.
  - ``render_for_prompt()``: devuelve un string Markdown formateado
    listo para inyectar en el prompt fase 2.

Diseño defensivo: si el YAML falta o está corrupto, el repositorio
arranca con 0 reglas y el render es vacío. La fase 2 sigue
funcionando (sin checklist, solo con su prompt base).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

import yaml

logger = logging.getLogger(__name__)


class RevisionRule:
    """Una regla individual cargada del YAML."""

    def __init__(
        self,
        *,
        rule_id: str,
        titulo: str,
        descripcion: str,
        ejemplo: str | None = None,
    ) -> None:
        self.id = rule_id
        self.titulo = titulo
        self.descripcion = descripcion.strip()
        self.ejemplo = ejemplo.strip() if ejemplo else None

    def render(self, index: int) -> str:
        """Renderiza la regla como bloque de texto en el prompt.

        Formato:
            ### N. titulo (id: foo)
            <descripcion>

            Ejemplo:
            <ejemplo>
        """
        lines: List[str] = []
        lines.append(f"### {index}. {self.titulo}  *(id: `{self.id}`)*")
        lines.append("")
        lines.append(self.descripcion)
        if self.ejemplo:
            lines.append("")
            lines.append("**Ejemplo:**")
            lines.append("")
            lines.append(self.ejemplo)
        return "\n".join(lines)


class RevisionRulesRepository:
    """Carga y expone las reglas de revisión definidas en YAML."""

    def __init__(self, yaml_path: str | Path) -> None:
        self._yaml_path = Path(yaml_path)
        self._rules: List[RevisionRule] = self._load()

    def _load(self) -> List[RevisionRule]:
        if not self._yaml_path.exists():
            logger.warning(
                "[revision-rules] Archivo no existe: %s. "
                "Se arrancará SIN reglas de revisión.",
                self._yaml_path,
            )
            return []

        try:
            with self._yaml_path.open("r", encoding="utf-8") as fp:
                raw: Any = yaml.safe_load(fp) or {}
        except Exception as exc:
            logger.exception(
                "[revision-rules] Error parseando YAML %s: %s. "
                "Se arrancará SIN reglas.",
                self._yaml_path,
                exc,
            )
            return []

        if not isinstance(raw, dict):
            logger.warning(
                "[revision-rules] YAML %s no es un dict raíz; ignorado.",
                self._yaml_path,
            )
            return []

        rules_raw = raw.get("reglas") or []
        if not isinstance(rules_raw, list):
            logger.warning(
                "[revision-rules] Clave 'reglas' no es lista en %s.",
                self._yaml_path,
            )
            return []

        rules: List[RevisionRule] = []
        for idx, item in enumerate(rules_raw):
            if not isinstance(item, dict):
                logger.warning(
                    "[revision-rules] Entrada #%d no es dict; omitida.",
                    idx,
                )
                continue
            rule_id = (item.get("id") or "").strip()
            titulo = (item.get("titulo") or "").strip()
            descripcion = item.get("descripcion") or ""
            ejemplo = item.get("ejemplo")
            if not rule_id or not titulo or not descripcion:
                logger.warning(
                    "[revision-rules] Entrada #%d incompleta "
                    "(id=%r titulo=%r descripcion vacía=%s); omitida.",
                    idx, rule_id, titulo, not descripcion,
                )
                continue
            rules.append(
                RevisionRule(
                    rule_id=rule_id,
                    titulo=titulo,
                    descripcion=descripcion,
                    ejemplo=ejemplo,
                )
            )

        logger.info(
            "[revision-rules] Cargadas %d reglas desde %s: %s",
            len(rules),
            self._yaml_path,
            ", ".join(r.id for r in rules) if rules else "(ninguna)",
        )
        return rules

    @property
    def count(self) -> int:
        return len(self._rules)

    @property
    def rule_ids(self) -> List[str]:
        return [r.id for r in self._rules]

    def render_for_prompt(self) -> str:
        """Texto formateado listo para inyectar en el prompt fase 2.

        Si no hay reglas, devuelve un mensaje neutro indicándolo (la
        IA igualmente sigue su prompt base sin checklist).
        """
        if not self._rules:
            return (
                "_(No hay reglas de revisión definidas en "
                "`config/revision_rules.yaml`. La revisión se hará "
                "solo con criterio general.)_"
            )

        blocks: List[str] = []
        for idx, rule in enumerate(self._rules, start=1):
            blocks.append(rule.render(idx))
        return "\n\n---\n\n".join(blocks)

    def reload(self) -> None:
        """Recarga las reglas desde disco. Útil para hot-reload sin
        reiniciar todo sv2."""
        self._rules = self._load()
