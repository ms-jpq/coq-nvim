from typing import AbstractSet, AsyncIterator, Mapping, MutableSet

from pynvim_pp.atomic import Atomic
from pynvim_pp.logging import suppress_and_log
from std2.string import removesuffix

from ...shared.executor import AsyncExecutor
from ...shared.runtime import Supervisor
from ...shared.runtime import Worker as BaseWorker
from ...shared.settings import RegistersClient
from ...shared.sql import BIGGEST_INT
from ...shared.types import Completion, Context, Doc, Edit, SnippetEdit, SnippetGrammar
from .db.database import RDB


async def _registers(names: AbstractSet[str]) -> Mapping[str, str]:
    atomic = Atomic()
    for name in names:
        atomic.call_function("getreg", (name,))
    contents = await atomic.commit(str)

    return {name: txt for name, txt in zip(names, contents)}


class Worker(BaseWorker[RegistersClient, None]):
    def __init__(
        self,
        ex: AsyncExecutor,
        supervisor: Supervisor,
        always_wait: bool,
        options: RegistersClient,
        misc: None,
    ) -> None:
        self._yanked: MutableSet[str] = {*options.words, *options.lines}
        self._db = RDB(
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
                with suppress_and_log():
                    yanked = {*self._yanked}
                    self._yanked.clear()
                    registers = await _registers(yanked)
                    self._db.periodical(
                        wordreg={
                            name: text
                            for name, text in registers.items()
                            if name in self._options.words
                        },
                        linereg={
                            name: text
                            for name, text in registers.items()
                            if name in self._options.lines
                        },
                    )

            await self._with_interrupt(cont())
            async with self._idle:
                await self._idle.wait()

    async def post_yank(self, regname: str, regsize: int) -> None:
        async def cont() -> None:
            if not regname and regsize >= self._options.max_yank_size:
                return

            name = regname or "0"
            if name in {*self._options.words, *self._options.lines}:
                self._yanked.add(name)

        await self._ex.submit(cont())

    async def _work(self, context: Context) -> AsyncIterator[Completion]:
        limit = (
            BIGGEST_INT
            if context.manual
            else self._options.max_pulls or self._supervisor.match.max_results
        )
        async with self._work_lock:
            before = removesuffix(context.line_before, suffix=context.syms_before)
            linewise = not before or before.isspace()
            words = self._db.select(
                linewise,
                match_syms=self._options.match_syms,
                opts=self._supervisor.match,
                word=context.words,
                sym=context.syms,
                limit=limit,
            )
            for word in words:
                edit = (
                    SnippetEdit(new_text=word.text, grammar=SnippetGrammar.lit)
                    if word.linewise
                    else Edit(new_text=word.text)
                )
                docline = f"{self._options.short_name}{self._options.register_scope}{word.regname}"
                doc = Doc(
                    text=docline,
                    syntax="",
                )
                cmp = Completion(
                    source=self._options.short_name,
                    always_on_top=self._options.always_on_top,
                    weight_adjust=self._options.weight_adjust,
                    label=edit.new_text,
                    sort_by=word.match,
                    primary_edit=edit,
                    adjust_indent=False,
                    doc=doc,
                    icon_match="Text",
                )
                yield cmp
