"""Public paid JSON API (/api/v1). Reads already-published data only; never mutates publication."""
from .routes import router

__all__ = ["router"]
