from collections import deque
from dataclasses import replace
from itertools import chain
from json import dumps
from textwrap import dedent
from typing import AsyncIterator, Iterable, Iterator, Optional, Sequence
from uuid import uuid4

from pynvim_pp.buffer import Buffer, ExtMark, ExtMarker
from pynvim_pp.lib import encode
from pynvim_pp.logging import log
from pynvim_pp.nvim import Nvim
from pynvim_pp.rpc_types import NvimError
from pynvim_pp.types import BufNamespace
from pynvim_pp.window import Window

from ...lang import LANG
from ...registry import rpc
from ...shared.types import TextTransform
from ...snippets.parsers.lexer import decode_mark_idx
from ..edit import EditInstruction, apply, reset_undolevels
from ..mark import NS
from ..rt_types import Stack
from ..state import State, state

_OG_IDX = str(uuid4())


async def _ls_marks(ns: BufNamespace, buf: Buffer) -> Sequence[ExtMark]:
    ordered = sorted(
        (
            replace(
                mark,
                marker=ExtMarker(decode_mark_idx(mark.marker) - 1),
                meta={**mark.meta, _OG_IDX: mark.marker},
            )
            for mark in await buf.get_extmarks(ns)
            if mark.end and mark.end >= mark.begin
        ),
        key=lambda m: (m.marker == 0, m.marker, m.begin, m.end),
    )

    return ordered


async def _del_marks(buf: Buffer, ns: BufNamespace, marks: Iterable[ExtMark]) -> None:
    it = (mark.meta[_OG_IDX] for mark in marks)
    await buf.del_extmarks(ns, markers=it)


async def _marks(ns: BufNamespace, win: Window, buf: Buffer) -> AsyncIterator[ExtMark]:
    cursor = await win.get_cursor()
    for idx, mark in enumerate(await _ls_marks(ns=ns, buf=buf)):
        if not idx and mark.begin == cursor and mark.end == cursor:
            await _del_marks(buf=buf, ns=ns, marks=(mark,))
        else:
            yield mark


def _trans(new_text: str, marks: Sequence[ExtMark]) -> Iterator[EditInstruction]:
    new_lines = new_text.splitlines()
    for mark in marks:
        if end := mark.end:
            yield EditInstruction(
                primary=False,
                begin=mark.begin,
                end=end,
                cursor_yoffset=0,
                cursor_xpos=-1,
                new_lines=new_lines,
            )


def _safexform(xform: Optional[TextTransform], text: str) -> str:
    if not xform:
        return text
    else:
        try:
            return "".join(chain.from_iterable(xform(text)))
        except Exception as e:
            log.warning("%s", e)
            return text


async def _single_mark(
    mark_applied_notify: bool,
    st: State,
    mark: ExtMark,
    marks: Sequence[ExtMark],
    ns: BufNamespace,
    win: Window,
    buf: Buffer,
) -> None:
    row, col = mark.begin
    await reset_undolevels()

    picked, cshift = False, 0
    if xform := st.text_trans.get(mark.marker):
        linesep = await buf.linefeed()
        init = xform(None)
        if isinstance(init, str):
            resp = await Nvim.input(
                question=LANG("expand marks", texts=init), default=""
            )
            text = resp if resp is not None else init
            new_resp = (
                _safexform(xform, text=text) or linesep.join(await mark.text()) or None
            )
        else:
            picked = True
            line = linesep.join(await mark.text())
            lines = tuple(xform(line))
            opts = {f"{i}: {line}": line for i, line in enumerate(lines, start=1)}

            if new_resp := await Nvim.input_list(opts):
                cshift = len(encode(new_resp))
    else:
        new_resp = ""

    try:
        await apply(buf, instructions=_trans(new_resp or "", marks=(mark,)))
        await Nvim.exec("startinsert")
        await win.set_cursor(row=row, col=col + cshift)
    except NvimError as e:
        msg = f"""
        bad mark location {mark}

        {e}
        """
        log.warning("%s", dedent(msg))
    else:
        await Nvim.exec("startinsert")
        state(inserted_pos=(row, col))
        if not picked and mark_applied_notify:
            msg = LANG("applied mark", marks_left=len(marks))
            await Nvim.write(msg)
    finally:
        await _del_marks(buf=buf, ns=ns, marks=(mark,))


async def _linked_marks(
    st: State,
    mark: ExtMark,
    linked: Sequence[ExtMark],
    ns: BufNamespace,
    win: Window,
    buf: Buffer,
) -> bool:
    marks = tuple(chain((mark,), linked))
    row, col = mark.begin
    place_holders = [await mark.text() for mark in marks]

    if (xform := st.text_trans.get(mark.marker)) and not isinstance(xform(None), str):
        linesep = await buf.linefeed()
        if lines := tuple(
            chain.from_iterable(xform(linesep.join(ls)) for ls in place_holders)
        ):
            opts = {f"{i}: {line}": line for i, line in enumerate(lines, start=1)}
            new_resp = await Nvim.input_list(opts)
        else:
            new_resp = None
    else:
        texts = dumps(place_holders, check_circular=False, ensure_ascii=False)
        resp = await Nvim.input(question=LANG("expand marks", texts=texts), default="")
        new_resp = _safexform(xform, text=resp) if resp is not None else None

    if new_resp is not None:
        try:
            await reset_undolevels()
            shift = await apply(buf, instructions=_trans(new_resp, marks=marks))
            await Nvim.exec("startinsert")
            await win.set_cursor(row=row + shift.row, col=col + len(encode(new_resp)))
            state(inserted_pos=(row, col - 1))
            return True
        finally:
            await _del_marks(buf, ns=ns, marks=marks)
    else:
        return False


@rpc()
async def nav_mark(stack: Stack) -> None:
    ns = await Nvim.create_namespace(NS)
    win = await Window.get_current()
    buf = await win.get_buf()

    if marks := deque([m async for m in _marks(ns=ns, win=win, buf=buf)]):
        s = state()
        mark = marks.popleft()

        async def single() -> None:
            mark_applied_notify = stack.settings.display.mark_applied_notify
            await _single_mark(
                mark_applied_notify, s, mark=mark, marks=marks, ns=ns, win=win, buf=buf
            )

        if linked := tuple(m for m in marks if m.marker == mark.marker):
            edited = await _linked_marks(
                s, mark=mark, linked=linked, ns=ns, win=win, buf=buf
            )
            if not edited:
                await single()
        else:
            await single()

    else:
        msg = LANG("no more marks")
        await Nvim.write(msg)
