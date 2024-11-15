from pathlib import Path, PurePath
from typing import AbstractSet, AsyncIterator, Mapping

from ...shared.executor import AsyncExecutor
from ...shared.runtime import Supervisor
from ...shared.runtime import Worker as BaseWorker
from ...shared.settings import SnippetClient
from ...shared.sql import BIGGEST_INT
from ...shared.types import Completion, Context, Doc, SnippetEdit, SnippetGrammar
from ...snippets.types import LoadedSnips
from .db.database import SDB


class Worker(BaseWorker[SnippetClient, Path]):
    def __init__(
        self,
        ex: AsyncExecutor,
        supervisor: Supervisor,
        options: SnippetClient,
        misc: Path,
    ) -> None:
        self._db = SDB(misc)
        super().__init__(ex, supervisor=supervisor, options=options, misc=misc)

    def interrupt(self) -> None:
        with self._interrupt():
            self._db.interrupt()

    async def db_mtimes(self) -> Mapping[PurePath, float]:
        async def cont() -> Mapping[PurePath, float]:
            with self._interrupt_lock:
                return self._db.mtimes()

        return await self._ex.submit(cont())

    async def clean(self, stale: AbstractSet[PurePath]) -> None:
        async def cont() -> None:
            with self._interrupt_lock:
                self._db.clean(stale)

        await self._ex.submit(cont())

    async def populate(self, path: PurePath, mtime: float, loaded: LoadedSnips) -> None:
        async def cont() -> None:
            with self._interrupt_lock:
                self._db.populate(path, mtime=mtime, loaded=loaded)

        await self._ex.submit(cont())

    async def _work(self, context: Context) -> AsyncIterator[Completion]:
        limit = (
            BIGGEST_INT
            if context.manual
            else self._options.max_pulls or self._supervisor.match.max_results
        )

        async with self._work_lock:
            snippets = self._db.select(
                self._supervisor.match,
                filetype=context.filetype,
                word=context.words,
                sym=context.syms,
                limit=limit,
            )

            for snip in snippets:
                edit = SnippetEdit(
                    new_text=snip["snippet"],
                    grammar=SnippetGrammar[snip["grammar"]],
                )
                label_line, *_ = (snip["label"] or edit.new_text or " ").splitlines()
                label = label_line.strip().expandtabs(context.tabstop)
                doc = Doc(text=snip["doc"] or edit.new_text, syntax=context.filetype)
                completion = Completion(
                    source=self._options.short_name,
                    always_on_top=self._options.always_on_top,
                    weight_adjust=self._options.weight_adjust,
                    primary_edit=edit,
                    adjust_indent=True,
                    sort_by=snip["word"],
                    label=label,
                    doc=doc,
                    kind=snip["word"],
                    icon_match="Snippet",
                )
                yield completion
