# infrastructure/storage/sharepoint_contrato_pdf_downloader.py
from __future__ import annotations

import base64
import logging
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import quote

import httpx

from domain.ports.contrato_pdf_downloader import (
    ContratoPdfDownloader,
    DownloadedContratoPdf,
)
from infrastructure.graph.token_provider import GraphTokenProvider

logger = logging.getLogger(__name__)

SharePointMode = Literal["drive_id", "folder_url", "site_path"]


class SharePointContratoPdfDownloader(ContratoPdfDownloader):
    """Descarga un PDF de SharePoint dado su ``relative_path``.

    Funciona en los mismos tres modos que el upload del servicio 3:
      - drive_id: usa directamente SHAREPOINT_DRIVE_ID.
      - folder_url: resuelve la carpeta raíz via /shares/{token}/driveItem.
      - site_path: resuelve site + drive por nombre.

    El ``relative_path`` que viene en BBDD es el path completo dentro
    del drive (p.e. "albaranes/2026/03/contratos/C-2025-001_123_foo.pdf").
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
        self._token_provider = GraphTokenProvider(graph_key, timeout_s)
        self._client = httpx.Client(timeout=timeout_s)
        self._base = "https://graph.microsoft.com/v1.0"
        self._mode: SharePointMode = mode
        self._hostname = (hostname or "").strip() or None
        self._site_path = (site_path or "").strip() or None
        self._drive_name = drive_name.strip()
        self._drive_id = (drive_id or "").strip() or None
        self._folder_url = (folder_url or "").strip() or None

        self._site_id_cache: str | None = None
        self._folder_drive_cache: str | None = None

        self._validate_mode_config()

    def _validate_mode_config(self) -> None:
        if self._mode == "drive_id" and not self._drive_id:
            raise RuntimeError(
                "SHAREPOINT_MODE=drive_id exige SHAREPOINT_DRIVE_ID."
            )
        if self._mode == "folder_url" and not self._folder_url:
            raise RuntimeError(
                "SHAREPOINT_MODE=folder_url exige SHAREPOINT_FOLDER_URL."
            )
        if self._mode == "site_path":
            if not self._hostname or not self._site_path:
                raise RuntimeError(
                    "SHAREPOINT_MODE=site_path exige SHAREPOINT_HOSTNAME y "
                    "SHAREPOINT_SITE_PATH."
                )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token_provider.get_token()}"}

    def _resolve_drive_id(self) -> str:
        if self._drive_id:
            return self._drive_id
        if self._mode == "folder_url":
            if self._folder_drive_cache:
                return self._folder_drive_cache
            token = self._encode_sharing_url(self._folder_url or "")
            url = f"{self._base}/shares/{token}/driveItem"
            response = self._client.get(url, headers=self._headers())
            if response.status_code >= 300:
                raise RuntimeError(
                    f"Graph get share driveItem {response.status_code}: "
                    f"{response.text[:500]}"
                )
            payload = response.json() or {}
            parent_ref = payload.get("parentReference") or {}
            drive_id = str(parent_ref.get("driveId") or "").strip()
            if not drive_id:
                raise RuntimeError(
                    "Graph no devolvió driveId al resolver SHAREPOINT_FOLDER_URL."
                )
            self._folder_drive_cache = drive_id
            return drive_id
        # mode == "site_path"
        assert self._hostname and self._site_path
        if self._site_id_cache is None:
            relative_path = self._site_path.lstrip("/")
            url = f"{self._base}/sites/{self._hostname}:/{relative_path}"
            response = self._client.get(url, headers=self._headers())
            if response.status_code >= 300:
                raise RuntimeError(
                    f"Graph get site by path {response.status_code}: "
                    f"{response.text[:500]}"
                )
            site_id = str((response.json() or {}).get("id") or "").strip()
            if not site_id:
                raise RuntimeError("Graph no devolvió site.id.")
            self._site_id_cache = site_id

        url = f"{self._base}/sites/{self._site_id_cache}/drives"
        response = self._client.get(url, headers=self._headers())
        if response.status_code >= 300:
            raise RuntimeError(
                f"Graph list drives {response.status_code}: "
                f"{response.text[:500]}"
            )
        items = (response.json() or {}).get("value") or []
        for item in items:
            if str(item.get("name") or "").strip() == self._drive_name:
                self._drive_id = str(item["id"])
                return self._drive_id
        raise RuntimeError(
            f"No se encontró la biblioteca SharePoint '{self._drive_name}'."
        )

    @staticmethod
    def _encode_sharing_url(url: str) -> str:
        raw = base64.b64encode(url.encode("utf-8")).decode("ascii")
        token = raw.rstrip("=").replace("/", "_").replace("+", "-")
        return f"u!{token}"

    def download_by_relative_path(
        self,
        *,
        relative_path: str,
    ) -> DownloadedContratoPdf:
        if not relative_path:
            raise ValueError("relative_path vacío.")
        drive_id = self._resolve_drive_id()
        # Normaliza el path: quitamos la posible carpeta raíz que ya
        # conoce SharePoint (ej. "Documentos compartidos/...").
        normalized = relative_path.replace("\\", "/").lstrip("/")
        encoded_path = quote(normalized, safe="/")
        url = f"{self._base}/drives/{drive_id}/root:/{encoded_path}:/content"
        logger.info(
            "[sp-download] GET %s", url[:200],
        )
        response = self._client.get(
            url, headers=self._headers(), follow_redirects=True,
        )
        if response.status_code == 302:
            # follow_redirects=True ya debería haberlo seguido, pero por si acaso
            location = response.headers.get("Location")
            if location:
                response = self._client.get(location)
        if response.status_code >= 300:
            raise RuntimeError(
                f"Graph download {response.status_code}: "
                f"{response.text[:500]}"
            )
        content = response.content or b""
        if not content:
            raise RuntimeError(
                f"Graph devolvió binario vacío para {relative_path}"
            )
        filename = PurePosixPath(normalized).name or "contrato.pdf"
        content_type = (
            response.headers.get("Content-Type") or "application/pdf"
        )
        return DownloadedContratoPdf(
            filename=filename,
            mime_type=content_type,
            data=content,
        )
