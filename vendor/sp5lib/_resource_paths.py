"""Auflösung der Ressourcen-Verzeichnisse der Host-Anwendung.

Historisch lagen diese Module im OpenSchichtplaner5-``backend/``-Baum und fanden
Nachbar-Ressourcen (``backend/data``, ``backend/api/data``, das Alembic-
Verzeichnis) über Pfade relativ zu ``__file__``. Als eigenständig installiertes
Paket (``libopenschichtplaner5``) gilt diese Annahme nicht mehr — das Paket
liegt in ``site-packages``, die Daten der Host-App woanders.

Die Host-Anwendung teilt der Bibliothek ihr Backend-Root daher über die
Umgebungsvariable ``SP5_BACKEND_DIR`` mit. Ungesetzt greift der Legacy-
Fallback ``<Paket-Elternverzeichnis>``, damit ein In-Tree-Checkout unverändert
funktioniert.
"""

from __future__ import annotations

import os


def backend_dir() -> str:
    """Liefert das Backend-Root-Verzeichnis der Host-Anwendung.

    Beachtet ``SP5_BACKEND_DIR``; sonst Fallback auf das Verzeichnis, das
    dieses Paket enthält (Legacy-In-Tree-Layout ``<backend>/sp5lib/``).
    """
    env = os.environ.get("SP5_BACKEND_DIR")
    if env:
        return os.path.abspath(env)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir() -> str:
    """``<backend>/data`` — JSON-backed settings (changelog, swaps, comments …)."""
    path = os.path.join(backend_dir(), "data")
    os.makedirs(path, exist_ok=True)
    return path


def api_data_dir() -> str:
    """``<backend>/api/data`` — Verfügbarkeits-/Skills-JSON der API-Schicht."""
    return os.path.join(backend_dir(), "api", "data")
