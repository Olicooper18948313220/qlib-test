from .leader import LeaderScanner
from .potent import PotentScanner

REGISTRY = {"leader": LeaderScanner, "potent": PotentScanner}
