from asyncio import Condition, as_completed
from typing import AsyncIterator, Optional

from pynvim_pp.logging import suppress_and_log
from std2 import anext
from std2.aitertools import to_async
from std2.itertools import batched

from ...consts import CACHE_CHUNK
from ...lsp.requests.completion import comp_lsp_inline
from ...lsp.types import LSPcomp
from ...shared.executor import AsyncExecutor
from ...shared.runtime import Supervisor
from ...shared.runtime import Worker as BaseWorker
from ...shared.settings import LSPInlineClient
from ...shared.timeit import timeit
from ...shared.types import Completion, Context
from ..cache.worker import CacheWorker


class Worker(BaseWorker[LSPInlineClient, None]):
    def __init__(
        self,
        ex: AsyncExecutor,
        supervisor: Supervisor,
        always_wait: bool,
        options: LSPInlineClient,
        misc: None,
    ) -> None:
        super().__init__(
            ex,
            supervisor=supervisor,
            always_wait=always_wait,
            options=options,
            misc=misc,
        )
        self._cache = CacheWorker(supervisor)
        self._working = Condition()
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
            async with self._working:
                await self._working.wait()

            async def cont() -> None:
                if context := self._supervisor.current_context:
                    with suppress_and_log(), timeit("LSP INLINE PULL"):
                        async for comps in self._request(context):
                            for chunked in batched(comps.items, n=CACHE_CHUNK):
                                self._cache.set_cache(
                                    {comps.client: chunked}, skip_db=True
                                )

            await self._with_interrupt(cont())

    async def _work(self, context: Context) -> AsyncIterator[Completion]:
        async with self._work_lock, self._working:
            try:
                _, _, cached = self._cache.apply_cache(context, always=True, inline_shift=True)
                lsp_stream = (
                    self._request(context)
                    if self._options.live_pulling
                    else to_async(())
                )

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
            finally:
                self._working.notify_all()
