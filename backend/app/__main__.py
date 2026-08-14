"""Point d'entrée : ``python -m app`` démarre le serveur Rythmo Dub.

Utilisé aussi en tant que fabrique ASGI : ``uvicorn app.__main__:appli``.
"""
from __future__ import annotations

import uvicorn

from .api import creer_app

appli = creer_app()


def main() -> None:
    uvicorn.run(appli, host="0.0.0.0", port=8000, log_level="info")


if __name__ == "__main__":
    main()
