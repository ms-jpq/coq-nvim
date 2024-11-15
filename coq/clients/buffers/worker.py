from dataclasses import dataclass
from os import linesep
from pathlib import PurePath
from typing import AsyncIterator, Iterator, Mapping, Optional, Sequence, Tuple

from pynvim_pp.buffer import Buffer
from pynvim_pp.logging import suppress_and_log
from pynvim_pp.rpc_types import NvimError
from pynvim_pp.window import Window

from ...paths.show import fmt_path
from ...shared.executor import AsyncExecutor
from ...shared.runtime import Supervisor
from ...shared.runtime import Worker as BaseWorker
from ...shared.settings import BuffersClient
from ...shared.sql import BIGGEST_INT
from ...shared.types import Completion, Context, Doc, Edit
from .db.database import BDB, BufferWord, Update


@dataclass(frozen=True)
class _Info:
    buf_id: int
    filetype: str
    filename: str
    range: Tuple[int, int]
    lines: Sequence[str]
    buffers: Mapping[Buffer, int]


async def _info() -> Optional[_Info]:
    try:
        win = await Window.get_current()
        height = await win.get_height()
        buf = await win.get_buf()
        bufs = await Buffer.list(listed=True)
        buffers = {buf: await buf.line_count() for buf in bufs}
        if (current_lines := buffers.get(buf)) is None:
            return None
        else:
            row, _ = await win.get_cursor()
            lo = max(0, row - height)
            hi = min(current_lines, row + height + 1)
            lines = await buf.get_lines(lo=lo, hi=hi)
            filetype = await buf.filetype()
            filename = (await buf.get_name()) or ""
            info = _Info(
                buf_id=buf.number,
                filetype=filetype,
                filename=filename,
                range=(lo, hi),
                lines=lines,
                buffers=buffers,
            )
            return info
    except NvimError:
        return None


def _doc(client: BuffersClient, context: Context, word: BufferWord) -> Doc:
    def cont() -> Iterator[str]:
        if not client.same_filetype and word.filetype:
            yield f"{word.filetype}{client.parent_scope}"

        path = PurePath(word.filename)
        pos = fmt_path(
            context.cwd, path=path, is_dir=False, current=PurePath(context.filename)
        )
        yield f"{pos}:{word.line_num}"

    return Doc(text=linesep.join(cont()), syntax="")


class Worker(BaseWorker[BuffersClient, None]):
    def __init__(
        self,
        ex: AsyncExecutor,
        supervisor: Supervisor,
        always_wait: bool,
        options: BuffersClient,
        misc: None,
    ) -> None:
        self._db = BDB(
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
                    if info := await _info():
                        lo, hi = info.range
                        buf_line_counts = {
                            int(buf.number): line_count
                            for buf, line_count in info.buffers.items()
                        }
                        self._db.vacuum(buf_line_counts)
                        self._db.set_lines(
                            info.buf_id,
                            filetype=info.filetype,
                            filename=info.filename,
                            lo=lo,
                            hi=hi,
                            lines=info.lines,
                        )

            await self._with_interrupt(cont())
            async with self._idle:
                await self._idle.wait()

    async def buf_update(self, buf_id: int, filetype: str, filename: str) -> None:
        async def cont() -> None:
            with self._interrupt_lock:
                self._db.buf_update(buf_id, filetype=filetype, filename=filename)

        await self._ex.submit(cont())

    async def set_lines(
        self,
        buf_id: int,
        filetype: str,
        filename: str,
        lo: int,
        hi: int,
        lines: Sequence[str],
    ) -> None:
        async def cont() -> None:
            self._db.set_lines(
                buf_id,
                filetype=filetype,
                filename=filename,
                lo=lo,
                hi=hi,
                lines=lines,
            )

        await self._ex.submit(cont())

    async def _work(self, context: Context) -> AsyncIterator[Completion]:
        limit = (
            BIGGEST_INT
            if context.manual
            else self._options.max_pulls or self._supervisor.match.max_results
        )

        async with self._work_lock:
            filetype = context.filetype if self._options.same_filetype else None
            update = (
                Update(
                    buf_id=context.buf_id,
                    filetype=context.filetype,
                    filename=context.filename,
                    lo=change.range.start,
                    hi=change.range.stop,
                    lines=change.lines,
                )
                if (change := context.change)
                else None
            )
            words = self._db.words(
                self._supervisor.match,
                filetype=filetype,
                word=context.words,
                sym=context.syms if self._options.match_syms else "",
                limit=limit,
                update=update,
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
                    doc=_doc(self._options, context=context, word=word),
                    icon_match="Text",
                )
                yield cmp
