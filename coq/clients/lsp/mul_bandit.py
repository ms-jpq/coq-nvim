from collections import defaultdict
from datetime import timedelta
from math import inf
from random import random
from typing import AbstractSet, MutableSet, Optional


class MultiArmedBandit:
    def __init__(self) -> None:
        self._probability_floor = 0.05
        self._clients: MutableSet[str] = set()

    def _elapsed(self, client: str) -> float:
        return inf

    def update(
        self, clients: AbstractSet[str], client: Optional[str], elapsed: timedelta
    ) -> None:
        self._clients |= clients
