"""Run the ClauseAI development server."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    """Start uvicorn with development-friendly defaults."""
    import uvicorn

    environment = os.getenv("ENVIRONMENT", "development").lower()
    port = int(os.getenv("PORT", "8000"))
    reload = environment == "development"
    uvicorn.run(
        "clauseai.main:app",
        host="0.0.0.0",
        port=port,
        reload=reload,
    )


if __name__ == "__main__":
    main()
