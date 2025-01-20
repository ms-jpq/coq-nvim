from bisect import bisect
from collections import defaultdict
from datetime import timedelta
from math import exp, floor, gamma, inf, log
from random import random, uniform
from typing import AbstractSet, MutableSet, Optional, Sequence

from std2.itertools import pairwise


def _logit(x: float) -> float:
    return log(x / (1 - x))


def _bins(k: float, n: int) -> Sequence[float]:
    return tuple((exp(k * i) - 1) * 1000 for i in range(n)) + (inf,)


class _Dist:
    def __init__(self) -> None:
        self._bins = _bins(k=0.03, n=6)
        self._decay = 0.99
        self._cdf = [0.0 for _ in pairwise(self._bins)]
        self._sum = 0.0

    def update(self, x: float) -> None:
        binned, inc = False, 0.0
        for i, (lo, hi) in enumerate(pairwise(self._bins)):
            binned |= lo <= x < hi
            inc += binned
            self._cdf[i] = self._cdf[i] * self._decay + binned
        self._cdf[-1] = self._cdf[-1] * self._decay + inc

    def inv_cdf(self, p: float) -> float:
        assert 0 <= p <= 1

        i = round((len(self._cdf) - 1) * p)
        print(self._cdf[i + 1])
        frac = (p - self._cdf[i]) / (self._cdf[i + 1] - self._cdf[i])
        return self._bins[i] + frac * (self._bins[i + 1] - self._bins[i])


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
