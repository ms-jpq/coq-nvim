from asyncio import as_completed
from typing import AsyncIterator, Optional

from pynvim_pp.logging import suppress_and_log
from std2 import anext
from std2.itertools import batched

from ...consts import CACHE_CHUNK
from ...lsp.requests.completion import comp_lsp_inline
from ...lsp.types import LSPcomp
from ...shared.executor import AsyncExecutor
from ...shared.runtime import Supervisor
from ...shared.runtime import Worker as BaseWorker
from ...shared.settings import LSPClient
from ...shared.timeit import timeit
from ...shared.types import Completion, Context
from ..cache.worker import CacheWorker


class Worker(BaseWorker[LSPClient, None]):
    def __init__(
        self,
        ex: AsyncExecutor,
        supervisor: Supervisor,
        options: LSPClient,
        misc: None,
    ) -> None:
        super().__init__(ex, supervisor=supervisor, options=options, misc=misc)
        self._cache = CacheWorker(supervisor)
        self._ex.run(self._poll())

    def interrupt(self) -> None:
        with self._interrupt():
            self._cache.interrupt()

    def _request(self, context: Context) -> AsyncIterator[LSPcomp]:
        return comp_lsp_inline(
            short_name=self._options.short_name,
            always_on_top=self._options.always_on_top,
            weight_adjust=self._options.weight_adjust,
            context=context,
            chunk=self._supervisor.match.max_results * 2,
            clients=set(),
        )

    async def _poll(self) -> None:
        while True:

            async def cont() -> None:
                if context := self._supervisor.current_context:
                    with suppress_and_log(), timeit("LSP INLINE PULL"):
                        async for comps in self._request(context):
                            for chunked in batched(comps.items, n=CACHE_CHUNK):
                                self._cache.set_cache({comps.client: chunked})

            await self._with_interrupt(cont())
            async with self._idle:
                await self._idle.wait()

    async def _work(self, context: Context) -> AsyncIterator[Completion]:
        async with self._work_lock:
            _, _, cached = self._cache.apply_cache(context)
            lsp_stream = self._request(context)

            async def db() -> LSPcomp:
                return LSPcomp(client=None, local_cache=False, items=cached)

            async def lsp() -> Optional[LSPcomp]:
                return await anext(lsp_stream, None)

            async def stream() -> AsyncIterator[LSPcomp]:
                for co in as_completed((db(), lsp())):
                    if comps := await co:
                        yield comps

                async for lsp_comps in lsp_stream:
                    yield lsp_comps

            async for comp in stream():
                for row in comp.items:
                    yield row
