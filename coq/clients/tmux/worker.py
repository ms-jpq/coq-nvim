from asyncio import Lock
from os import linesep
from pathlib import Path
from typing import AsyncIterator, Iterator

from pynvim_pp.logging import suppress_and_log

from ...shared.executor import AsyncExecutor
from ...shared.runtime import Supervisor
from ...shared.runtime import Worker as BaseWorker
from ...shared.settings import TmuxClient
from ...shared.sql import BIGGEST_INT
from ...shared.timeit import timeit
from ...shared.types import Completion, Context, Doc, Edit
from ...tmux.parse import snapshot
from .db.database import TMDB, TmuxWord


def _doc(client: TmuxClient, word: TmuxWord) -> Doc:
    def cont() -> Iterator[str]:
        if client.all_sessions:
            yield f"S: {word.session_name}{client.parent_scope}"
        yield f"W: #{word.window_index}{client.path_sep}{word.window_name}{client.parent_scope}"
        yield f"P: #{word.pane_index}{client.path_sep}{word.pane_title}"

    return Doc(text=linesep.join(cont()), syntax="")


class Worker(BaseWorker[TmuxClient, Path]):
    def __init__(
        self,
        ex: AsyncExecutor,
        supervisor: Supervisor,
        always_wait: bool,
        options: TmuxClient,
        misc: Path,
    ) -> None:
        self._exec = misc
        self._lock = Lock()
        self._db = TMDB(
            supervisor.limits.tokenization_limit,
            unifying_chars=supervisor.match.unifying_chars,
            include_syms=options.match_syms,
        )
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

            async def cont() -> None:
                with suppress_and_log(), timeit("IDLE :: TMUX"):
                    await self._periodical()

            await self._with_interrupt(cont())
            async with self._idle:
                await self._idle.wait()

    async def _periodical(self) -> None:
        if not self._lock.locked():
            async with self._lock:
                current, panes = await snapshot(
                    self._exec, all_sessions=self._options.all_sessions
                )
                self._db.periodical(current, panes=panes)

    async def periodical(self) -> None:
        await self._ex.submit(self._periodical())

    async def _work(self, context: Context, timeout: float) -> AsyncIterator[Completion]:
        limit = (
            BIGGEST_INT
            if context.manual
            else self._options.max_pulls or self._supervisor.match.max_results
        )
        async with self._work_lock:
            words = self._db.select(
                self._supervisor.match,
                word=context.words,
                sym=(context.syms if self._options.match_syms else ""),
                limit=limit,
            )

            for word in words:
                edit = Edit(new_text=word.text)
                cmp = Completion(
                    source=self._options.short_name,
                    always_on_top=self._options.always_on_top,
                    weight_adjust=self._options.weight_adjust,
                    label=edit.new_text,
                    sort_by=word.text,
                    primary_edit=edit,
                    adjust_indent=False,
                    doc=_doc(self._options, word=word),
                    icon_match="Text",
                )
                yield cmp
