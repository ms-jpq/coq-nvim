from typing import AsyncIterator

from ...lsp.requests.completion import comp_thirdparty
from ...lsp.types import LSPcomp
from ...shared.types import Context
from ..lsp.worker import Worker as LSPWorker


class Worker(LSPWorker):
    def _request(self, context: Context) -> AsyncIterator[LSPcomp]:
        return comp_thirdparty(
            short_name=self._options.short_name,
            always_on_top=self._options.always_on_top,
            weight_adjust=self._options.weight_adjust,
            context=context,
            chunk=self._max_results * 2,
            clients=set(),
        )
