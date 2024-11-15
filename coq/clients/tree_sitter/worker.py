from asyncio import Lock, gather
from os import linesep
from pathlib import PurePath
from typing import AsyncIterator, Iterator, Mapping, Optional, Tuple

from pynvim_pp.atomic import Atomic
from pynvim_pp.buffer import Buffer
from pynvim_pp.logging import suppress_and_log
from pynvim_pp.rpc_types import NvimError

from ...paths.show import fmt_path
from ...shared.executor import AsyncExecutor
from ...shared.runtime import Supervisor
from ...shared.runtime import Worker as BaseWorker
from ...shared.settings import TSClient
from ...shared.sql import BIGGEST_INT
from ...shared.types import Completion, Context, Doc, Edit
from ...treesitter.request import async_request
from ...treesitter.types import Payload
from .db.database import TDB


async def _bufs() -> Mapping[int, int]:
    try:
        bufs = await Buffer.list(listed=True)
        atomic = Atomic()
        for buf in bufs:
            atomic.buf_line_count(buf)
        linecounts = await atomic.commit(int)
        counts = {
            int(buf.number): linecount for buf, linecount in zip(bufs, linecounts)
        }
        return counts
    except NvimError:
        return {}


def _doc(client: TSClient, context: Context, payload: Payload) -> Optional[Doc]:
    def cont() -> Iterator[str]:
        clhs, crhs = context.comment

        path = PurePath(context.filename)
        pos = fmt_path(
            context.cwd, path=PurePath(payload.filename), is_dir=False, current=path
        )

        yield clhs
        yield pos
        yield ":"
        lo, hi = payload.range
        yield str(lo)
        if hi != lo:
            yield "-"
            yield str(hi)
        yield client.path_sep
        yield crhs
        yield linesep

        if payload.grandparent:
            yield clhs
            yield payload.grandparent.kind
            yield linesep
            yield payload.grandparent.text
            yield crhs

        if payload.grandparent and payload.parent:
            yield linesep
            yield clhs
            yield client.path_sep
            yield crhs
            yield linesep

        if payload.parent:
            yield clhs
            yield payload.parent.kind
            yield linesep
            yield payload.parent.text
            yield crhs

    doc = Doc(syntax=context.filetype, text="".join(cont()))
    return doc


def _trans(client: TSClient, context: Context, payload: Payload) -> Completion:
    edit = Edit(new_text=payload.text)
    icon_match, _, _ = payload.kind.partition(".")
    cmp = Completion(
        source=client.short_name,
        always_on_top=client.always_on_top,
        weight_adjust=client.weight_adjust,
        label=edit.new_text,
        sort_by=payload.text,
        primary_edit=edit,
        adjust_indent=False,
        kind=payload.kind,
        doc=_doc(client, context=context, payload=payload),
        icon_match=icon_match,
    )
    return cmp


class Worker(BaseWorker[TSClient, None]):
    def __init__(
        self,
        ex: AsyncExecutor,
        supervisor: Supervisor,
        always_wait: bool,
        options: TSClient,
        misc: None,
    ) -> None:
        self._lock = Lock()
        self._db = TDB()
        super().__init__(
            ex,
            supervisor=supervisor,
            always_wait=always_wait,
            options=options,
            misc=misc,
        )
        self._ex.run(self._poll())

    def interrupt(self) -> None:
        with self._interrupt():
            self._db.interrupt()

    async def _poll(self) -> None:
        while True:
            async with self._idle:
                await self._idle.wait()

            async def cont() -> None:
                with suppress_and_log():
                    bufs, _ = await gather(_bufs(), self._populate())
                    if bufs:
                        self._db.vacuum(bufs)

            await self._with_interrupt(cont())

    async def _populate(self) -> Optional[Tuple[bool, float]]:
        if not self._lock.locked():
            async with self._lock:
                if payload := await async_request():
                    keep_going = payload.elapsed <= self._options.slow_threshold
                    self._db.populate(
                        payload.buf,
                        lo=payload.lo,
                        hi=payload.hi,
                        filetype=payload.filetype,
                        filename=payload.filename,
                        nodes=payload.payloads,
                    )
                    return keep_going, payload.elapsed

        return None

    async def populate(self) -> Optional[Tuple[bool, float]]:
        return await self._ex.submit(self._populate())

    async def _work(self, context: Context) -> AsyncIterator[Completion]:
        limit = (
            BIGGEST_INT
            if context.manual
            else self._options.max_pulls or self._supervisor.match.max_results
        )
        async with self._work_lock:
            payloads = self._db.select(
                self._supervisor.match,
                filetype=context.filetype,
                word=context.words,
                sym=context.syms,
                limit=limit,
            )

            for payload in payloads:
                yield _trans(self._options, context=context, payload=payload)
