# domain/ports/contrato_pdf_downloader.py
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class DownloadedContratoPdf:
    filename: str
    mime_type: str
    data: bytes


class ContratoPdfDownloader(ABC):
    """Puerto para descargar el PDF de un contrato desde el storage."""

    @abstractmethod
    def download_by_relative_path(
        self,
        *,
        relative_path: str,
    ) -> DownloadedContratoPdf:
        raise NotImplementedError
