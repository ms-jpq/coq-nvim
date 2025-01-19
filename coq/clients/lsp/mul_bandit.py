from collections import defaultdict
from datetime import timedelta
from math import exp, gamma, inf, log
from random import random, uniform
from typing import AbstractSet, MutableSet, Optional


def _kumaraswamy_inv_cdf(y: float, alpha: float, beta: float) -> float:
    """
    https://en.wikipedia.org/wiki/Kumaraswamy_distribution
    """

    return (1 - (1 - y) ** (1 - beta)) ** (1 / alpha)


def _random_variate_sampling(a: float, b: float) -> float:

    u = uniform(0, 1)
    return _kumaraswamy_inv_cdf(u, alpha=a, beta=b)


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
