"""ASGI entry: `uvicorn metacoin_service.asgi:app` with settings from the environment."""
from .api import create_app
from .config import Settings

app = create_app(Settings.from_env())
