from collections import defaultdict, deque
from dataclasses import dataclass, replace
from itertools import chain
from pprint import pformat
from string import Template
from textwrap import dedent
from typing import (
    Deque,
    Iterable,
    Iterator,
    MutableMapping,
    MutableSequence,
    Optional,
    Sequence,
    Tuple,
    cast,
)
from uuid import uuid4

from pynvim_pp.atomic import Atomic
from pynvim_pp.buffer import Buffer
from pynvim_pp.lib import decode, encode
from pynvim_pp.logging import log
from pynvim_pp.nvim import Nvim
from pynvim_pp.rpc_types import NvimError
from pynvim_pp.types import NoneType
from pynvim_pp.window import Window
from std2.collections import defaultlist
from std2.itertools import intervals
from std2.types import never

from ..consts import DEBUG
from ..lang import LANG
from ..shared.parse import coalesce
from ..shared.runtime import Metric
from ..shared.settings import CompleteOptions, MatchOptions
from ..shared.trans import indent_adjusted, trans_adjusted
from ..shared.types import (
    UTF8,
    UTF16,
    UTF32,
    BaseRangeEdit,
    Completion,
    Context,
    ContextualEdit,
    Edit,
    Mark,
    NvimPos,
    SnippetEdit,
    SnippetRangeEdit,
    TextTransforms,
)
from ..snippets.parse import ParsedEdit, parse_basic, parse_ranged
from ..snippets.parsers.types import ParseError, ParseInfo
from .mark import mark
from .rt_types import Stack
from .state import State

NS = uuid4()


@dataclass(frozen=True)
class EditInstruction:
    primary: bool
    begin: NvimPos
    end: NvimPos
    cursor_yoffset: int
    cursor_xpos: int
    new_lines: Sequence[str]


@dataclass(frozen=True)
class _Lines:
    lines: Sequence[str]
    b_lines8: Sequence[bytes]
    b_lines16: Sequence[bytes]
    b_lines32: Sequence[bytes]
    len8: Sequence[int]


# TODO -- column too
@dataclass(frozen=True)
class _MarkShift:
    row: int


@dataclass(frozen=True)
class _Parsed:
    instructions: Sequence[EditInstruction]
    marks: Sequence[Mark]
    text_trans: TextTransforms


def _lines(rows: Iterable[Tuple[range, Sequence[str]]]) -> _Lines:
    lines: MutableMapping[int, str] = defaultdict(str)
    b_lines8: MutableMapping[int, bytes] = defaultdict(bytes)
    b_lines16: MutableMapping[int, bytes] = defaultdict(bytes)
    b_lines32: MutableMapping[int, bytes] = defaultdict(bytes)
    len8: MutableMapping[int, int] = defaultdict(int)

    for row, ls in rows:
        assert len(row) == len(ls)
        for idx, line in zip(row, ls):
            b_line8 = encode(line)
            lines[idx] = line
            b_lines8[idx] = b_line8
            b_lines16[idx] = encode(line, encoding=UTF16)
            b_lines32[idx] = encode(line, encoding=UTF32)
            len8[idx] = len(b_line8)

    return _Lines(
        lines=defaultlist(lines),
        b_lines8=defaultlist(b_lines8),
        b_lines16=defaultlist(b_lines16),
        b_lines32=defaultlist(b_lines32),
        len8=defaultlist(len8),
    )


def _rows_to_fetch(ctx: Context, edit: Edit, *edits: Edit) -> Sequence[range]:
    row, _ = ctx.position

    def c1() -> Iterator[range]:
        for e in chain((edit,), edits):
            if isinstance(e, ContextualEdit):
                lo = row - (len(e.old_prefix.split(ctx.linefeed)) - 1)
                hi = row + (len(e.old_suffix.split(ctx.linefeed)) - 1)
                yield range(lo, hi)

            elif isinstance(e, BaseRangeEdit):
                (lo, _), (hi, _) = e.begin, e.end
                yield range(lo, hi)

            elif isinstance(e, Edit):
                yield range(row, row + 1)

            else:
                never(e)

    def c2() -> Iterator[range]:
        for row in c1():
            stop = min(ctx.line_count, row.stop + len(edits) + 1)
            yield range(row.start, stop)

    return tuple(intervals(tuple(c2())))


def _contextual_edit_trans(
    ctx: Context, adjust_indent: bool, lines: _Lines, edit: ContextualEdit
) -> EditInstruction:
    row, col = ctx.position
    old_prefix_lines = edit.old_prefix.split(ctx.linefeed)
    old_suffix_lines = edit.old_suffix.split(ctx.linefeed)

    r1 = row - (len(old_prefix_lines) - 1)
    r2 = row + (len(old_suffix_lines) - 1)

    c1 = (
        lines.len8[r1] - len(encode(old_prefix_lines[0]))
        if len(old_prefix_lines) > 1
        else col - len(encode(old_prefix_lines[0]))
    )
    c2 = (
        len(encode(old_suffix_lines[-1]))
        if len(old_suffix_lines) > 1
        else col + len(encode(old_suffix_lines[0]))
    )

    begin = r1, c1
    end = r2, c2

    split_lines = edit.new_text.split(ctx.linefeed)
    if adjust_indent:
        new_lines: Sequence[str] = tuple(
            indent_adjusted(ctx, line_before=ctx.line_before, lines=split_lines)
        )
    else:
        new_lines = split_lines

    new_prefix_lines = edit.new_prefix.split(ctx.linefeed)
    cursor_yoffset = -len(old_prefix_lines) + len(new_prefix_lines)
    cursor_xpos = (
        len(encode(new_prefix_lines[-1]))
        if len(new_prefix_lines) > 1
        else len(encode(ctx.line_before))
        - len(encode(old_prefix_lines[-1]))
        + len(encode(new_prefix_lines[0]))
    )

    inst = EditInstruction(
        primary=True,
        begin=begin,
        end=end,
        cursor_yoffset=cursor_yoffset,
        cursor_xpos=cursor_xpos,
        new_lines=new_lines,
    )
    return inst


def _edit_trans(
    match: MatchOptions,
    comp: CompleteOptions,
    adjust_indent: bool,
    ctx: Context,
    lines: _Lines,
    edit: Edit,
) -> EditInstruction:
    adjusted = trans_adjusted(match, comp=comp, ctx=ctx, new_text=edit.new_text)
    inst = _contextual_edit_trans(
        ctx, adjust_indent=adjust_indent, lines=lines, edit=adjusted
    )
    return inst


def _range_edit_trans(
    match: MatchOptions,
    comp: CompleteOptions,
    adjust_indent: bool,
    ctx: Context,
    primary: bool,
    lines: _Lines,
    edit: BaseRangeEdit,
) -> EditInstruction:
    if (
        primary
        and not isinstance(edit, ParsedEdit)
        and edit.begin == edit.end
        and len(
            tuple(
                coalesce(
                    match.unifying_chars,
                    include_syms=True,
                    backwards=None,
                    chars=edit.new_text,
                )
            )
        )
        > 1
    ):
        return _edit_trans(
            match,
            comp=replace(comp, replace_suffix_threshold=1),
            adjust_indent=adjust_indent,
            ctx=ctx,
            lines=lines,
            edit=edit,
        )

    else:
        (r1, ec1), (r2, ec2) = sorted((edit.begin, edit.end))
        split_lines = edit.new_text.split(ctx.linefeed)

        if edit.encoding == UTF16:
            c1 = len(encode(decode(lines.b_lines16[r1][: ec1 * 2], encoding=UTF16)))
            c2 = len(encode(decode(lines.b_lines16[r2][: ec2 * 2], encoding=UTF16)))
        elif edit.encoding == UTF8 or edit.encoding == UTF32:
            c1 = len(lines.b_lines8[r1][:ec1])
            c2 = len(lines.b_lines8[r2][:ec2])
        elif edit.encoding == UTF32:  # type: ignore
            # ccls says its utf-32, but its utf-8(ish)
            c1 = len(encode(decode(lines.b_lines32[r1][: ec1 * 4], encoding=UTF32)))
            c2 = len(encode(decode(lines.b_lines32[r2][: ec2 * 4], encoding=UTF32)))
            never(edit.encoding)
        else:
            never(edit.encoding)

        c1 = min(len(lines.b_lines8[r1]), c1)
        c2 = min(len(lines.b_lines8[r2]), c2)
        begin = r1, c1
        end = r2, c2

        if primary and adjust_indent:
            line_before = ctx.line_before[:c1]
            new_lines: Sequence[str] = tuple(
                indent_adjusted(ctx, line_before=line_before, lines=split_lines)
            )
        else:
            new_lines = split_lines

        lines_before = (
            edit.new_prefix.split(ctx.linefeed)
            if isinstance(edit, ParsedEdit)
            else new_lines
        )
        cursor_yoffset = (r2 - r1) + (len(lines_before) - 1)
        cursor_xpos = (
            (
                len(encode(lines_before[-1]))
                if len(lines_before) > 1
                else len(lines.b_lines8[r2][:c1]) + len(encode(lines_before[0]))
            )
            if primary
            else -1
        )

        inst = EditInstruction(
            primary=primary,
            begin=begin,
            end=end,
            cursor_yoffset=cursor_yoffset,
            cursor_xpos=cursor_xpos,
            new_lines=tuple(new_lines),
        )
        return inst


def _instructions(
    ctx: Context,
    match: MatchOptions,
    comp: CompleteOptions,
    adjust_indent: bool,
    lines: _Lines,
    primary: Edit,
    secondary: Sequence[BaseRangeEdit],
) -> Iterator[EditInstruction]:
    if isinstance(primary, BaseRangeEdit):
        inst = _range_edit_trans(
            match,
            comp=comp,
            adjust_indent=adjust_indent,
            ctx=ctx,
            primary=True,
            lines=lines,
            edit=primary,
        )
        yield inst

    elif isinstance(primary, ContextualEdit):
        inst = _contextual_edit_trans(
            ctx, adjust_indent=adjust_indent, lines=lines, edit=primary
        )
        yield inst

    elif isinstance(primary, Edit):
        inst = _edit_trans(
            match,
            comp=comp,
            adjust_indent=adjust_indent,
            ctx=ctx,
            lines=lines,
            edit=primary,
        )
        yield inst

    else:
        never(primary)

    for edit in secondary:
        yield _range_edit_trans(
            match,
            comp=comp,
            adjust_indent=adjust_indent,
            ctx=ctx,
            primary=False,
            lines=lines,
            edit=edit,
        )


def _consolidate(
    instruction: EditInstruction, *instructions: EditInstruction
) -> Sequence[EditInstruction]:
    edits = sorted(chain((instruction,), instructions), key=lambda i: (i.begin, i.end))
    pivot = 0, 0
    stack: MutableSequence[EditInstruction] = []

    for edit in edits:
        if edit.begin >= pivot:
            stack.append(edit)
            pivot = edit.end

        elif edit.primary:
            acc: Deque[EditInstruction] = deque()
            while stack:
                conflicting = stack.pop()
                acc.appendleft(conflicting)
                if conflicting.end <= edit.begin:
                    break
                else:
                    acc.popleft()
            stack.extend(acc)
            stack.append(edit)
            pivot = edit.end

        else:
            pass

    return stack


def _shift(
    instructions: Iterable[EditInstruction],
) -> Tuple[Sequence[EditInstruction], _MarkShift]:
    row_shift = 0
    cols_shift: MutableMapping[int, int] = {}

    m_shift = _MarkShift(row=0)
    new_insts: MutableSequence[EditInstruction] = []
    for inst in instructions:
        (r1, c1), (r2, c2) = inst.begin, inst.end
        new_inst = EditInstruction(
            primary=inst.primary,
            begin=(r1 + row_shift, c1 + cols_shift.get(r1, 0)),
            end=(r2 + row_shift, c2 + cols_shift.get(r2, 0)),
            cursor_yoffset=inst.cursor_yoffset,
            cursor_xpos=inst.cursor_xpos,
            new_lines=inst.new_lines,
        )
        if new_inst.primary:
            m_shift = _MarkShift(row=row_shift)

        row_shift += inst.cursor_yoffset
        f_length = len(encode(inst.new_lines[-1])) if inst.new_lines else 0
        cols_shift[r2] = -(c2 - c1) + f_length if r1 == r2 else -c2 + f_length

        new_insts.append(new_inst)

    return new_insts, m_shift


async def apply(buf: Buffer, instructions: Iterable[EditInstruction]) -> _MarkShift:
    insts, m_shift = _shift(instructions)
    for inst in insts:
        try:
            await buf.set_text(begin=inst.begin, end=inst.end, text=inst.new_lines)
        except NvimError as e:
            tpl = """
            ${e}
            ${inst}
            ${ctx}
            """

            (r1, _), (r2, _) = inst.begin, inst.end
            try:
                ctx = await buf.get_lines(min(r1, r2), max(r1, r2) + 1)
            except NvimError:
                ctx = []

            msg = Template(dedent(tpl)).substitute(e=e, inst=inst, ctx=ctx)
            log.warning("%s", msg)

    return m_shift


def _shift_marks(shift: _MarkShift, marks: Iterable[Mark]) -> Iterator[Mark]:
    for mark in marks:
        (r1, c1), (r2, c2) = mark.begin, mark.end
        new_mark = Mark(
            idx=mark.idx,
            begin=(r1 + shift.row, c1),
            end=(r2 + shift.row, c2),
            text=mark.text,
        )
        yield new_mark


def _cursor(cursor: NvimPos, instructions: Iterable[EditInstruction]) -> NvimPos:
    row, _ = cursor
    col = -1

    for inst in instructions:
        row += inst.cursor_yoffset
        col = inst.cursor_xpos
        if inst.primary:
            break

    assert col != -1
    return row, col


async def _parse(
    buf: Buffer, stack: Stack, state: State, comp: Completion, preview: bool
) -> Tuple[bool, Edit, Sequence[Mark], TextTransforms]:
    if isinstance(comp.primary_edit, SnippetEdit):
        comment_str = (None if preview else await buf.commentstr()) or ("", "")
        clipboard = "" if preview else await Nvim.fn.getreg(str)
        info = ParseInfo(visual="", clipboard=clipboard, comment_str=comment_str)
        if isinstance(comp.primary_edit, SnippetRangeEdit):
            row, col = comp.primary_edit.begin
            line, *_ = (
                (state.context.line,)
                if preview
                else await buf.get_lines(lo=row, hi=row + 1)
            )
            line_before = decode(
                encode(line, encoding=comp.primary_edit.encoding)[:col]
            )
            edit, marks, text_trans = parse_ranged(
                context=state.context,
                adjust_indent=comp.adjust_indent,
                snippet=comp.primary_edit,
                info=info,
                line_before=line_before,
            )
        else:
            edit, marks, text_trans = parse_basic(
                stack.settings.match,
                comp=stack.settings.completion,
                adjust_indent=comp.adjust_indent,
                context=state.context,
                snippet=comp.primary_edit,
                info=info,
            )
        adjusted = True
    else:
        adjusted = False
        edit, marks, text_trans = comp.primary_edit, (), {}

    return adjusted, edit, marks, text_trans


async def _view(
    buf: Buffer, state: State, comp: Completion, preview: bool
) -> Optional[_Lines]:
    rows = _rows_to_fetch(state.context, comp.primary_edit, *comp.secondary_edits)
    atomic = Atomic()

    for row in rows:
        if row.start < 0 or row.stop > state.context.line_count:
            log.warning("%s", pformat(("BAD ROW", row, comp)))
            return None
        else:
            atomic.buf_get_lines(buf, row.start, row.stop, True)

    try:
        lines = cast(Sequence[Sequence[str]], await atomic.commit(NoneType))
    except NvimError as e:
        log.warning("%s", e)
        return None
    else:
        return _lines(zip(rows, lines))


async def parse(
    buf: Buffer, stack: Stack, state: State, comp: Completion, preview: bool
) -> Optional[_Parsed]:
    cmp = replace(comp, secondary_edits=()) if preview else comp
    try:
        adjusted, primary, marks, text_trans = await _parse(
            buf=buf, stack=stack, state=state, comp=cmp, preview=preview
        )
    except (NvimError, ParseError) as e:
        adjusted, primary, marks, text_trans = (
            False,
            cmp.primary_edit,
            (),
            {},
        )
        await Nvim.write(LANG("failed to parse snippet"))
        log.info("%s", e)

    adjust_indent = cmp.adjust_indent and not adjusted
    if not (view := await _view(buf, state=state, comp=cmp, preview=preview)):
        return None
    else:
        instructions = _consolidate(
            *_instructions(
                state.context,
                match=stack.settings.match,
                comp=stack.settings.completion,
                adjust_indent=adjust_indent,
                lines=view,
                primary=primary,
                secondary=cmp.secondary_edits,
            )
        )

        return _Parsed(
            instructions=instructions,
            marks=marks,
            text_trans=text_trans,
        )


async def parse_secondary(
    buf: Buffer, stack: Stack, state: State, comp: Completion
) -> Iterator[EditInstruction]:
    cmp = replace(comp, primary_edit=Edit(new_text=""))
    if not (view := await _view(buf, state=state, comp=cmp, preview=True)):
        return iter(())
    else:
        instructions = _consolidate(
            *_instructions(
                state.context,
                match=stack.settings.match,
                comp=stack.settings.completion,
                adjust_indent=True,
                lines=view,
                primary=cmp.primary_edit,
                secondary=comp.secondary_edits,
            )
        )
        return (inst for inst in instructions if not inst.primary)


async def _restore(win: Window, buf: Buffer, pos: NvimPos) -> Tuple[str, Optional[int]]:
    row, _ = pos
    ns = await Nvim.create_namespace(NS)
    marks = await buf.get_extmarks(ns)
    if len(marks) != 2:
        return "", 0
    else:
        m1, m2 = marks
        after, *_ = await buf.get_lines(lo=row, hi=row + 1)
        cur_row, cur_col = await win.get_cursor()
        assert m1.end
        (_, lo), (_, hi) = m1.end, m2.begin

        binserted = encode(after)[lo:hi]
        inserted = decode(binserted)

        movement = cur_col - lo if cur_row == row and lo <= cur_col <= hi else None

        if inserted:
            await buf.set_text(begin=m1.end, end=m2.begin, text=("",))

        return inserted, movement


async def reset_undolevels() -> None:
    undolevels = await Nvim.opts.get(int, "undolevels")
    await Nvim.opts.set("undolevels", val=undolevels)


async def edit(
    stack: Stack,
    state: State,
    metric: Metric,
    synthetic: bool,
) -> Optional[Tuple[NvimPos, Optional[TextTransforms]]]:
    win = await Window.get_current()
    buf = await win.get_buf()
    if buf.number != state.context.buf_id:
        log.warning("%s", "stale buffer")
        return None
    else:
        await reset_undolevels()

        if synthetic:
            inserted, movement = "", None
        else:
            inserted, movement = await _restore(
                win=win, buf=buf, pos=state.context.position
            )

        if not (
            parsed := await parse(
                buf=buf, stack=stack, state=state, comp=metric.comp, preview=False
            )
        ):
            return None

        n_row, n_col = _cursor(
            state.context.position,
            instructions=parsed.instructions,
        )

        if not synthetic:
            stack.idb.inserted(metric.instance.bytes, sort_by=metric.comp.sort_by)

        m_shift = await apply(buf=buf, instructions=parsed.instructions)
        if inserted:
            try:
                await buf.set_text(
                    begin=(n_row, n_col),
                    end=(n_row, n_col),
                    text=(inserted,),
                )
            except NvimError as e:
                log.warning("%s", e)

        if movement is not None:
            try:
                await win.set_cursor(row=n_row, col=n_col + movement)
            except NvimError as e:
                log.warning("%s", e)

        if new_marks := tuple(_shift_marks(m_shift, marks=parsed.marks)):
            await mark(settings=stack.settings, buf=buf, marks=new_marks)

        if DEBUG:
            log.debug("%s", pformat((metric, parsed.instructions)))

        return (n_row, n_col), parsed.text_trans
