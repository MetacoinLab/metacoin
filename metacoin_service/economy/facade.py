"""One entry point for the economy services, constructed by api.Services."""
from .service import Terms
from .board import Providers, Board
from .evidence import Evidence


class Economy:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services
        self.terms = Terms(settings, services)
        self.providers = Providers(settings, services)
        self.board = Board(settings, services, self.providers)
        self.evidence = Evidence(settings, services, self.board)

    def tick(self, db):
        """Scheduler pass (worker loop): request expiry, attempts/milestones from jobs, receipts, dispute deadlines."""
        n = self.board.expire_requests(db)
        n += self.evidence.tick(db)
        n += self.evidence.expire_disputes(db)
        return n
