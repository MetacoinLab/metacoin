"""One entry point for the economy services, constructed by api.Services."""
from .service import Terms
from .board import Providers, Board
from .evidence import Evidence
from .money import Money
from .treasury import Treasury
from .access import Access
from .missions import Missions
from .programs import Programs
from .pricing import Pricing
from .challenges import Challenges


class Economy:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services
        self.terms = Terms(settings, services)
        self.providers = Providers(settings, services)
        self.board = Board(settings, services, self.providers)
        self.evidence = Evidence(settings, services, self.board)
        self.money = Money(settings, services, self.board, self.evidence)
        self.treasury = Treasury(settings, services, self.money)
        self.access = Access(settings, services, self.board, self.evidence)
        self.missions = Missions(settings, services, self.board, self.evidence)
        self.programs = Programs(settings, services)
        self.pricing = Pricing(settings, services)
        self.challenges = Challenges(settings, services)
        self.board.programs = self.programs
        self.evidence.challenges = self.challenges
        self.evidence.money = self.money
        self.evidence.access = self.access
        self.evidence.missions = self.missions
        self.board.treasury = self.treasury
        self.board.evidence = self.evidence

    def tick(self, db):
        """Scheduler pass (worker loop): request expiry, attempts/milestones from jobs, receipts, dispute deadlines."""
        n = self.board.expire_requests(db)
        n += self.evidence.tick(db)
        n += self.evidence.expire_disputes(db)
        return n
