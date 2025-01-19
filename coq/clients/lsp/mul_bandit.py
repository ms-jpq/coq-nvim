from datetime import timedelta
from typing import AbstractSet, Optional


class MultiArmedBandit:
    def __init__(self):
        pass

    def update(
        self, clients: AbstractSet[str], client: Optional[str], elapsed: timedelta
    ) -> None:
        pass
