"""One entry point for the economy services, constructed by api.Services."""
from .service import Terms


class Economy:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services
        self.terms = Terms(settings, services)
