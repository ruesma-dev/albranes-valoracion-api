# infrastructure/storage/sharepoint_contrato_pdf_downloader.py
"""Descarga el PDF de contrato desde SharePoint para la valoración.

La mecánica Graph (resolución de drive en los 3 modos, descarga por path
relativo, follow de redirect) vive ahora en el cliente común
``ruesma_comun.sharepoint.GraphSharePointClient``. Este adaptador solo
añade la API de dominio del servicio 5: ``download_by_relative_path``
devolviendo un ``DownloadedContratoPdf``.

Requiere: pip install -e ../comun
"""
from __future__ import annotations

import logging
from pathlib import PurePosixPath

from domain.ports.contrato_pdf_downloader import (
    ContratoPdfDownloader,
    DownloadedContratoPdf,
)
from ruesma_comun.sharepoint import GraphSharePointClient, SharePointMode

logger = logging.getLogger(__name__)


class SharePointContratoPdfDownloader(GraphSharePointClient, ContratoPdfDownloader):
    """Descarga un PDF de SharePoint dado su ``relative_path``.

    Funciona en los mismos tres modos que el upload del servicio 3
    (drive_id · folder_url · site_path). El ``relative_path`` que viene
    en BBDD es el path completo dentro del drive (p. ej.
    "albaranes/2026/03/contratos/C-2025-001_123_foo.pdf").
    """

    def __init__(
        self,
        *,
        graph_key: str,
        timeout_s: int,
        mode: SharePointMode,
        hostname: str | None,
        site_path: str | None,
        drive_name: str,
        drive_id: str | None,
        folder_url: str | None = None,
    ) -> None:
        super().__init__(
            graph_key=graph_key,
            timeout_s=timeout_s,
            mode=mode,
            hostname=hostname,
            site_path=site_path,
            drive_name=drive_name,
            drive_id=drive_id,
            folder_url=folder_url,
        )

    def download_by_relative_path(
        self,
        *,
        relative_path: str,
    ) -> DownloadedContratoPdf:
        if not relative_path:
            raise ValueError("relative_path vacío.")
        drive_id = self._resolve_drive_id_all_modes()
        content, content_type = self._download_bytes_by_relative_path(
            drive_id=drive_id,
            relative_path=relative_path,
        )
        normalized = relative_path.replace("\\", "/").lstrip("/")
        filename = PurePosixPath(normalized).name or "contrato.pdf"
        return DownloadedContratoPdf(
            filename=filename,
            mime_type=content_type,
            data=content,
        )

    def download_markdown_by_relative_path(
        self,
        *,
        relative_path: str,
    ) -> str:
        """Descarga el Markdown del contrato (generado por sv3) como texto.

        Es lo que la valoración usa preferentemente: el MD va al ``user_text``
        de la IA en modo texto puro, en vez del PDF como adjunto.
        """
        if not relative_path:
            raise ValueError("relative_path vacío.")
        drive_id = self._resolve_drive_id_all_modes()
        return self._download_text_by_relative_path(
            drive_id=drive_id,
            relative_path=relative_path,
        )
