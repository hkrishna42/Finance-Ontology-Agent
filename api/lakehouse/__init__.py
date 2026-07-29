"""Simulated internal lakehouse (medallion: bronze -> silver -> gold) master-data store."""

from .store import bootstrap, init_lakehouse_db

__all__ = ["bootstrap", "init_lakehouse_db"]
