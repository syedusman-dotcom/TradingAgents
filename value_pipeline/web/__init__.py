"""The value pipeline's local website (uv run python -m value_pipeline web)."""

from value_pipeline.web.app import create_app

__all__ = ["create_app"]
