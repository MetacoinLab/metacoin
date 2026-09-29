"""One entry point for the economy services, constructed by api.Services."""
from .service import Terms
from .board import Providers, Board


class Economy:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services
        self.terms = Terms(settings, services)
        self.providers = Providers(settings, services)
        self.board = Board(settings, services, self.providers)
