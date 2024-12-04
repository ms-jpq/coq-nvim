from asyncio import Condition, as_completed, sleep
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import (
    AbstractSet,
    AsyncIterator,
    Iterator,
    MutableMapping,
    MutableSequence,
    Optional,
    Tuple,
)

from pynvim_pp.logging import suppress_and_log
from std2 import anext
from std2.itertools import batched

from ...consts import CACHE_CHUNK
from ...lsp.requests.completion import comp_lsp
from ...lsp.types import LSPcomp
from ...shared.context import cword_before
from ...shared.executor import AsyncExecutor
from ...shared.fuzzy import multi_set_ratio
from ...shared.parse import lower
from ...shared.runtime import Supervisor
from ...shared.runtime import Worker as BaseWorker
from ...shared.settings import LSPClient, MatchOptions
from ...shared.sql import BIGGEST_INT
from ...shared.timeit import timeit
from ...shared.types import Completion, Context, SnippetEdit
from ..cache.worker import CacheWorker, sanitize_cached


class _Src(Enum):
    from_db = auto()
    from_stored = auto()
    from_query = auto()


def _use_comp(
    match: MatchOptions, context: Context, sort_by: str, comp: Completion
) -> bool:
    cword = cword_before(
        match.unifying_chars,
        lower=True,
        context=context,
        sort_by=sort_by,
    )

    if len(sort_by) + match.look_ahead >= len(cword):
        ratio = multi_set_ratio(
            cword,
            lower(sort_by),
            look_ahead=match.look_ahead,
        )
        use = ratio >= match.fuzzy_cutoff and (
            isinstance(comp.primary_edit, SnippetEdit)
            or bool(comp.secondary_edits)
            or bool(comp.extern)
            or not cword.startswith(comp.primary_edit.new_text)
        )
        return use
    else:
        return False


@dataclass(frozen=True)
class _LocalCache:
    pre: MutableMapping[Optional[str], Iterator[Completion]] = field(
        default_factory=dict
    )
    post: MutableMapping[Optional[str], MutableSequence[Completion]] = field(
        default_factory=dict
    )


class Worker(BaseWorker[LSPClient, None]):
    def __init__(
        self,
        ex: AsyncExecutor,
        supervisor: Supervisor,
        always_wait: bool,
        options: LSPClient,
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
        self._local_cached = _LocalCache()
        self._working = Condition()
        self._max_results = self._supervisor.match.max_results
        self._ex.run(self._poll())

    def interrupt(self) -> None:
        with self._interrupt():
            self._cache.interrupt()

    def _request(
        self, context: Context, cached_clients: AbstractSet[str]
    ) -> AsyncIterator[LSPcomp]:
        return comp_lsp(
            short_name=self._options.short_name,
            always_on_top=self._options.always_on_top,
            weight_adjust=self._options.weight_adjust,
            context=context,
            chunk=self._max_results,
            clients=set() if context.manual else cached_clients,
        )

    async def _poll(self) -> None:
        while True:
            async with self._working:
                await self._working.wait()

            async def cont() -> None:
                with suppress_and_log(), timeit("LSP CACHE"):
                    if not self._work_lock.locked():
                        self._cache.set_cache(self._local_cached.post, skip_db=False)
                        acc = tuple(self._local_cached.pre.items())
                        for client, comps in acc:
                            await sleep(0)
                            if not self._work_lock.locked():
                                for chunked in batched(comps, n=CACHE_CHUNK):
                                    self._cache.set_cache(
                                        {client: chunked}, skip_db=False
                                    )
                        if context := self._supervisor.current_context:
                            async for lsp_comps in self._request(
                                context, cached_clients=frozenset()
                            ):
                                if not self._work_lock.locked():
                                    for chunked in batched(
                                        lsp_comps.items, n=CACHE_CHUNK
                                    ):
                                        self._cache.set_cache(
                                            {lsp_comps.client: chunked}, skip_db=False
                                        )

            await self._with_interrupt(cont())

    async def _work(self, context: Context) -> AsyncIterator[Completion]:
        limit = (
            BIGGEST_INT
            if context.manual
            else self._options.max_pulls or self._supervisor.match.max_results
        )

        async with self._work_lock, self._working:
            try:
                use_cache, cached_clients, cached = self._cache.apply_cache(
                    context, always=False
                )
                if not use_cache:
                    self._local_cached.pre.clear()
                    self._local_cached.post.clear()

                lsp_stream = self._request(context, cached_clients=cached_clients)

                async def db() -> Tuple[_Src, LSPcomp]:
                    return _Src.from_db, LSPcomp(
                        client=None, local_cache=False, items=cached
                    )

                async def lsp() -> Optional[Tuple[_Src, LSPcomp]]:
                    if comps := await anext(lsp_stream, None):
                        return _Src.from_query, comps
                    else:
                        return None

                async def stream() -> AsyncIterator[Tuple[_Src, LSPcomp]]:
                    acc = {**self._local_cached.pre}
                    self._local_cached.pre.clear()

                    for client, cached_items in acc.items():
                        items = (
                            cached
                            for item in cached_items
                            if (
                                cached := sanitize_cached(
                                    context.cursor, comp=item, sort_by=None
                                )
                            )
                        )
                        yield _Src.from_stored, LSPcomp(
                            client=client, local_cache=True, items=items
                        )

                    for co in as_completed((db(), lsp())):
                        if comps := await co:
                            yield comps

                    async for lsp_comps in lsp_stream:
                        yield _Src.from_query, lsp_comps

                seen = 0
                async for src, lsp_comps in stream():
                    if seen >= limit:
                        break

                    acc = self._local_cached.post.setdefault(lsp_comps.client, [])

                    if lsp_comps.local_cache and src is not _Src.from_db:
                        self._local_cached.pre[lsp_comps.client] = lsp_comps.items

                    for comp in lsp_comps.items:
                        if src is _Src.from_db:
                            seen += 1
                            yield comp
                        else:
                            acc.append(comp)
                            if _use_comp(
                                self._supervisor.match,
                                context=context,
                                sort_by=comp.sort_by,
                                comp=comp,
                            ):
                                seen += 1
                                yield comp
            finally:
                self._working.notify_all()
