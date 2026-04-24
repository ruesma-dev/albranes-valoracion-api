# infrastructure/database/session_factory.py
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


class SessionFactory:
    """Factoría de sesiones SQLAlchemy en modo solo-lectura lógica.

    Este servicio NO crea BBDD ni tablas: asume que svc 3 ya lo hizo.
    Se usa exclusivamente para leer líneas de albarán y contrato.
    """

    def __init__(self, database_url: str) -> None:
        self._engine: Engine = create_engine(
            database_url,
            future=True,
            pool_pre_ping=True,
        )
        self._sessionmaker = sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            future=True,
        )

    @property
    def engine(self) -> Engine:
        return self._engine

    def create_session(self) -> Session:
        return self._sessionmaker()
