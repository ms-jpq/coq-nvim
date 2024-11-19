from asyncio import create_task
from contextlib import suppress
from dataclasses import dataclass, replace
from functools import lru_cache
from html import unescape
from itertools import chain
from math import ceil
from os import linesep
from textwrap import dedent
from typing import Any, Awaitable, Callable, Iterator, Mapping, Sequence, Tuple
from uuid import UUID, uuid4

from pynvim_pp.buffer import Buffer, ExtMark, ExtMarker
from pynvim_pp.float_win import border_w_h, list_floatwins
from pynvim_pp.lib import display_width, encode
from pynvim_pp.logging import suppress_and_log
from pynvim_pp.nvim import Nvim
from pynvim_pp.preview import buf_set_preview, set_preview
from pynvim_pp.rpc_types import NvimError
from pynvim_pp.types import NoneType
from pynvim_pp.window import Window
from std2 import anext, clamp
from std2.asyncio import Cancellation
from std2.pickle.decoder import new_decoder
from std2.pickle.types import DecodeError
from std2.string import removeprefix

from ...lsp.requests.resolve import resolve
from ...paths.show import show
from ...registry import NAMESPACE, autocmd, rpc
from ...shared.aio import with_timeout
from ...shared.settings import GhostText, PreviewDisplay
from ...shared.timeit import timeit
from ...shared.trans import expand_tabs, indent_adjusted
from ...shared.types import Completion, Context, Doc, ExternLSP, ExternPath
from ..edit import EditInstruction, parse, parse_secondary
from ..rt_types import Stack
from ..state import State, state

_FLOAT_WIN_UUID = uuid4()
_NS = uuid4()

_die = Cancellation()


@dataclass(frozen=True)
class _Event:
    completed_item: Mapping[str, Any]
    row: int
    col: int
    height: int
    width: int
    size: int
    scrollbar: bool


@dataclass(frozen=True)
class _Pos:
    row: int
    col: int
    height: int
    width: int


@rpc()
async def _kill_win(stack: Stack, reset: bool) -> None:
    if reset:
        state(pum_location=None, preview_id=uuid4())

    buf = await Buffer.get_current()
    ns = await Nvim.create_namespace(_NS)
    await buf.clear_namespace(ns)

    async for win in list_floatwins(_FLOAT_WIN_UUID):
        await win.close()


_ = (
    autocmd("CompleteDone", "InsertLeave")
    << f"lua {NAMESPACE}.{_kill_win.method}(true)"
)


def _preprocess(context: Context, doc: Doc) -> Doc:
    sep = "```"
    if doc.syntax == "markdown":
        esc_text = unescape(doc.text)
        split = esc_text.splitlines()

        if (
            split
            and split[0].startswith(sep)
            and split[-1].startswith(sep)
            and not sum(line.startswith(sep) for line in split[1:-1])
        ):
            text = linesep.join(split[1:-1])
            ft = removeprefix(split[0], prefix=sep).strip()
            syntax = ft if ft.isalnum() else context.filetype
            return Doc(text=text, syntax=syntax)
        else:
            return Doc(text=esc_text, syntax=doc.syntax)
    else:
        return doc


def _clamp(hi: int) -> Callable[[int], int]:
    return lambda i: clamp(1, i, hi)


def _positions(
    display: PreviewDisplay,
    event: _Event,
    lines: Sequence[str],
    state: State,
) -> Iterator[Tuple[int, int, _Pos]]:
    scr_width, scr_height = state.screen
    top, btm, left, right = (
        event.row,
        event.row + event.height + 1,
        event.col,
        event.col + event.width + event.scrollbar,
    )
    dls = tuple(display_width(line, tabsize=state.context.tabstop) for line in lines)
    limit_w = _clamp(min(display.x_max_len, max(chain((0,), dls))))
    limit_h = _clamp(sum(ceil((dl or 1) / display.x_max_len) for dl in dls))

    ns_width = limit_w(scr_width - left)
    n_height = limit_h(top - 1)
    b_width, b_height = border_w_h(display.border)

    ns_col = left - 1
    n = _Pos(
        row=top - 1 - n_height - b_height,
        col=ns_col,
        height=n_height,
        width=ns_width,
    )
    if n.row > 1 and display.positions.north is not None:
        yield 1, display.positions.north, n

    s = _Pos(
        row=btm,
        col=ns_col,
        height=limit_h(scr_height - btm),
        width=ns_width,
    )

    if s.row + s.height < scr_height - 1 and display.positions.south is not None:
        yield 2, display.positions.south, s

    we_height = limit_h(scr_height - top - 2)
    w_width = limit_w(left - 2)

    w = _Pos(
        row=top,
        col=left - 2 - w_width - b_width,
        height=we_height,
        width=w_width,
    )

    if display.positions.west is not None:
        yield 3, display.positions.west, w

    e = _Pos(
        row=top,
        col=right + 1,
        height=we_height,
        width=limit_w(scr_width - right - 2),
    )

    if display.positions.east is not None:
        yield 4, display.positions.east, e


async def _set_win(display: PreviewDisplay, buf: Buffer, pos: _Pos) -> None:
    opts = {
        "relative": "editor",
        "anchor": "NW",
        "style": "minimal",
        "noautocmd": True,
        "width": pos.width,
        "height": pos.height,
        "row": pos.row,
        "col": pos.col,
        "border": display.border,
    }
    win = await Nvim.api.open_win(Window, buf, False, opts)
    await win.opts.set("wrap", val=True)
    await win.vars.set(str(_FLOAT_WIN_UUID), val=True)


async def _show_preview(stack: Stack, event: _Event, doc: Doc, s: State) -> None:
    new_doc = _preprocess(s.context, doc=doc)
    text = dedent(expand_tabs(s.context, text=new_doc.text))
    lines = text.splitlines()
    pit = _positions(stack.settings.display.preview, event=event, lines=lines, state=s)

    def key(k: Tuple[int, int, _Pos]) -> Tuple[int, int, int, int]:
        idx, rank, pos = k
        return pos.height * pos.width, idx == s.pum_location, -rank, -idx

    if ordered := sorted(pit, key=key, reverse=True):
        (pum_location, _, pos), *__ = ordered
        state(pum_location=pum_location)
        buf = await Buffer.create(
            listed=False, scratch=True, wipe=True, nofile=True, noswap=True
        )
        await buf_set_preview(buf=buf, syntax=new_doc.syntax, preview=lines)
        with suppress(NvimError):
            await Nvim.api.exec_lua(
                NoneType,
                f"{NAMESPACE}.treesitter_start(...)",
                (buf, new_doc.syntax or "markdown"),
            )
        await _set_win(display=stack.settings.display.preview, buf=buf, pos=pos)


async def _secondary(stack: Stack, state: State, comp: Completion) -> Doc:
    buf = await Buffer.get_current()
    instructions = await parse_secondary(buf, stack=stack, state=state, comp=comp)
    text = linesep.join(
        linesep.join(f"+ {line}" for line in inst.new_lines) for inst in instructions
    )
    return Doc(text=text, syntax="")


async def _resolve_comp(
    stack: Stack,
    event: _Event,
    state: State,
    comp: Completion,
) -> None:
    @_die
    async def cont() -> None:
        enabled = stack.settings.display.preview.enabled
        timeout = stack.settings.display.preview.resolve_timeout
        with suppress_and_log():
            if cached := stack.lru.get(state.preview_id):
                doc = cached.doc
            else:
                if isinstance(comp.extern, ExternLSP):
                    if cmp := await with_timeout(
                        timeout,
                        co=resolve(extern=comp.extern),
                    ):
                        stack.lru[state.preview_id] = cmp
                    doc = (cmp.doc if cmp else None) or comp.doc
                elif isinstance(comp.extern, ExternPath) and enabled:
                    if doc := await with_timeout(
                        timeout,
                        co=show(
                            cwd=state.cwd,
                            path=comp.extern.path,
                            ellipsis=stack.settings.display.pum.ellipsis,
                            height=stack.settings.clients.paths.preview_lines,
                        ),
                    ):
                        stack.lru[state.preview_id] = replace(comp, doc=doc)
                else:
                    doc = None

            if enabled and (not doc or not doc.text and comp.secondary_edits):
                doc = await with_timeout(
                    timeout,
                    co=_secondary(stack, state=state, comp=comp),
                )
                stack.lru[state.preview_id] = replace(comp, doc=doc)

            if enabled and doc and doc.text:
                await _show_preview(
                    stack=stack,
                    event=event,
                    doc=doc,
                    s=state,
                )

    create_task(cont())


async def _virt_text(
    stack: Stack, state: State, ghost: GhostText, comp: Completion
) -> None:
    context = state.context
    if ghost.enabled and comp.primary_edit.new_text:
        supports_vlines = await Nvim.api.has("nvim-0.8")

        ns = await Nvim.create_namespace(_NS)
        win = await Window.get_current()
        buf = await win.get_buf()
        row, col = await win.get_cursor()
        parsed = await parse(
            buf,
            stack=stack,
            state=state,
            comp=comp,
            preview=True,
        )
        instruction, *_ = (
            parsed
            and parsed.instructions
            or (
                EditInstruction(
                    primary=True,
                    begin=(row, col),
                    end=(row, col),
                    new_lines=tuple(
                        indent_adjusted(
                            context,
                            line_before=context.line_before,
                            lines=comp.primary_edit.new_text.splitlines(),
                        )
                    ),
                    cursor_xpos=-1,
                    cursor_yoffset=0,
                ),
            )
        )
        inline, *rest_lines = instruction.new_lines or ("",)
        b_row, b_col = instruction.begin
        e_row, e_col = instruction.end

        bb_col = b_col if b_row == row else col
        ee_col = e_col if e_row == row else len(encode(context.line_before))
        begin, end = (row, bb_col), (row, max(0, ee_col - 1))

        mark = ExtMark(
            buf=buf,
            marker=ExtMarker(1),
            begin=begin,
            end=end,
            meta={
                "virt_text_pos": "overlay",
                "hl_mode": "combine",
                "virt_text": ((inline, ghost.highlight_group),),
            },
        )

        def marks() -> Iterator[ExtMark]:
            yield mark
            if supports_vlines and rest_lines:
                vlines = tuple(((line, ghost.highlight_group),) for line in rest_lines)
                yield ExtMark(
                    buf=buf,
                    marker=ExtMarker(3),
                    begin=begin,
                    end=begin,
                    meta={
                        "virt_text_pos": "overlay",
                        "hl_mode": "combine",
                        "virt_lines": vlines,
                    },
                )

        await buf.clear_namespace(ns)
        await buf.set_extmarks(ns, extmarks=marks())


_DECODER = new_decoder[_Event](_Event)
_UDECODER = new_decoder[UUID](UUID)


@rpc(schedule=True)
async def _cmp_changed(stack: Stack, event: Mapping[str, Any] = {}) -> None:
    await _kill_win(stack=stack, reset=False)
    with timeit("PREVIEW"):
        try:
            ev = _DECODER(event)
            user_data = ev.completed_item.get("user_data", "")
            uid = _UDECODER(user_data)
        except DecodeError:
            pass
        else:
            if metric := stack.metrics.get(uid):
                s = state()
                await _virt_text(
                    stack,
                    state=s,
                    ghost=stack.settings.display.ghost_text,
                    comp=metric.comp,
                )
                s = state(preview_id=uid)
                if metric.comp.extern:
                    await _resolve_comp(
                        stack=stack,
                        event=ev,
                        state=s,
                        comp=metric.comp,
                    )
                elif metric.comp.doc and metric.comp.doc.text:
                    await _show_preview(
                        stack=stack,
                        event=ev,
                        doc=metric.comp.doc,
                        s=s,
                    )


_ = autocmd("CompleteChanged") << f"lua {NAMESPACE}.{_cmp_changed.method}(vim.v.event)"


@lru_cache(maxsize=None)
def _escaped() -> Awaitable[str]:
    return create_task(Nvim.api.replace_termcodes(str, "<c-e>", True, False, True))


@rpc()
async def preview_preview(stack: Stack, *_: str) -> str:
    if win := await anext(list_floatwins(_FLOAT_WIN_UUID), None):
        buf = await win.get_buf()
        syntax = await buf.opts.get(str, "syntax")
        lines = await buf.get_lines()
        await Nvim.exec("stopinsert")

        async def cont() -> None:
            with suppress_and_log():
                await set_preview(syntax=syntax, preview=lines)

        create_task(cont())

    return await _escaped()
