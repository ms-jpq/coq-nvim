from itertools import chain
from string import Template
from textwrap import dedent
from typing import (
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    MutableSequence,
    NoReturn,
    Sequence,
    Tuple,
    Union,
)

from pynvim_pp.lib import encode
from pynvim_pp.logging import log
from std2.itertools import deiter, interleave
from std2.types import never

from ...shared.types import Context, TextTransform
from ..consts import MOD_PAD, SNIP_LINE_SEP
from .types import (
    EChar,
    End,
    Index,
    IntBegin,
    Parsed,
    ParseError,
    ParseInfo,
    ParserCtx,
    Region,
    Token,
    TokenStream,
    Transform,
    Unparsed,
    VarBegin,
)


def raise_err(
    text: str, pos: Index, condition: str, expected: Iterable[str], actual: str
) -> NoReturn:
    band = 5
    char = f"'{actual}'" if actual else "EOF"
    expected_chars = ", ".join(map(lambda c: f"'{c}'", expected))
    ctx = "" if pos.i == -1 else text[pos.i - band : pos.i + band + 1]
    tpl = """
    Unexpected char found :: `${condition}`:
    row:  ${row}
    col:  ${col}
    Expected one of: > ${expected_chars} <
    Found:           ${char}
    Context: |-
    ${ctx}
    Text:    |-
    ${text}
    """
    msg = Template(dedent(tpl)).substitute(
        condition=condition,
        row=pos.row,
        col=pos.col,
        expected_chars=expected_chars,
        char=char,
        ctx=ctx,
        text=text,
    )
    raise ParseError(msg)


def next_char(it: Iterator[EChar]) -> EChar:
    return next(it, (Index(i=-1, row=-1, col=-1), ""))


def pushback_chars(context: ParserCtx, *vals: EChar) -> None:
    for pos, char in reversed(vals):
        if char:
            context.dit.push_back((pos, char))


def _gen_iter(src: str) -> Iterator[EChar]:
    row, col = 1, 1
    for i, c in enumerate(
        chain.from_iterable(interleave(src.splitlines(), val=(SNIP_LINE_SEP,)))
    ):
        yield Index(i=i, row=row, col=col), c
        col += 1
        if c == SNIP_LINE_SEP:
            row += 1
            col = 0


def context_from(snippet: str, context: Context, info: ParseInfo) -> ParserCtx:
    dit = deiter(_gen_iter(snippet))
    ctx = ParserCtx(ctx=context, text=snippet, info=info, dit=dit, stack=[])
    return ctx


def _overlap(r1: Region, r2: Region) -> bool:
    r1_inside_r2 = r1.begin >= r2.begin and r1.end <= r2.end
    r2_inside_r1 = r2.begin >= r1.begin and r2.end <= r1.end
    return r1_inside_r2 or r2_inside_r1


def _consolidate(
    text: str, regions: Mapping[int, Sequence[Region]]
) -> Iterator[Tuple[int, Region]]:
    new_regions = (
        (
            r.end - r.begin,
            idx == 0,
            idx,
            Region(begin=r.begin, end=r.end, text=text[r.begin : r.end]),
        )
        for idx, rs in regions.items()
        for r in rs
    )
    ordered = sorted(new_regions, key=lambda t: t[:-1])

    acc: MutableMapping[int, MutableSequence[Region]] = {}
    for _, _, idx, region in ordered:
        if overlapped := tuple(
            (region, reg)
            for reg in chain.from_iterable(acc.values())
            if _overlap(region, reg)
        ):
            log.warning("%s", f"snippet region overlapped -- {overlapped}")
        else:
            a = acc.setdefault(idx, [])
            a.append(region)

    for idx, rs in acc.items():
        for i, region in enumerate(rs, start=len(rs) > 1):
            yield idx + MOD_PAD * i, region


def decode_mark_idx(idx: int) -> int:
    return idx % MOD_PAD


def token_parser(context: ParserCtx, stream: TokenStream) -> Parsed:
    idx = 0
    raw_regions: MutableMapping[int, MutableSequence[Region]] = {}
    slices: MutableSequence[str] = []
    begins: MutableSequence[Tuple[int, Union[IntBegin, VarBegin]]] = []
    xforms: MutableMapping[int, TextTransform] = {}
    bad_tokens: MutableSequence[Tuple[int, Token]] = []

    trans_idx = -1
    x_token = Transform(var_subst=None, maybe_idx=-1, xform=lambda _: ())

    for token in stream:
        if isinstance(token, Unparsed):
            token = token
            bad_tokens.append((idx, token))
        elif isinstance(token, str):
            idx += len(encode(token))
            slices.append(token)
        elif isinstance(token, IntBegin):
            begins.append((idx, token))
        elif isinstance(token, Transform):
            if token.var_subst is not None:
                trans_idx, x_token = idx, token
            else:
                xforms[token.maybe_idx] = token.xform
        elif isinstance(token, VarBegin):
            begins.append((idx, token))
        elif isinstance(token, End):
            if begins:
                pos, begin = begins.pop()
                if isinstance(begin, IntBegin):
                    acc = raw_regions.setdefault(begin.idx, [])
                    acc.append(Region(begin=pos, end=idx, text=""))

                    if (
                        begin.idx == x_token.maybe_idx
                        and idx == trans_idx
                        and pos == trans_idx - len(x_token.var_subst or "")
                    ):
                        xforms[x_token.maybe_idx] = x_token.xform
            else:
                bad_tokens.append((idx, token))
        else:
            never(token)

    bad_tokens.extend(begins)
    text = "".join(slices)
    min_key = min(raw_regions.keys(), key=lambda i: (i == 0, i)) if raw_regions else -1
    cursor = next(
        iter(raw_regions.get(min_key, ())),
        Region(begin=len(encode(text)), end=0, text=""),
    ).begin

    if bad_tokens:
        tpl = """
        Bad tokens :: Most likely unbalanced `{…}` - ${bad_tokens}
        Parsed: |-
        ${text}
        Original: |-
        ${ctx}
        """
        msg = Template(dedent(tpl)).substitute(
            bad_tokens=bad_tokens, text=text, ctx=context.text
        )
        raise ParseError(msg)

    regions = tuple(_consolidate(text, regions=raw_regions))
    parsed = Parsed(text=text, cursor=cursor, regions=regions, xforms=xforms)
    return parsed
