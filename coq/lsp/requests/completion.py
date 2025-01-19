from datetime import timedelta
from typing import AbstractSet, AsyncIterator, Optional, Tuple, cast

from ...shared.types import Context, ExternLSP, ExternLUA
from ..parse import parse, parse_inline
from ..protocol import protocol
from ..types import CompletionResponse, InLineCompletionResponse, LSPcomp
from .request import async_request

_Rsp = Tuple[LSPcomp, AbstractSet[str], timedelta]


async def comp_lsp(
    short_name: str,
    always_on_top: Optional[AbstractSet[Optional[str]]],
    weight_adjust: float,
    context: Context,
    chunk: int,
    clients: AbstractSet[str],
) -> AsyncIterator[_Rsp]:
    pc = await protocol()

    async for client in async_request("lsp_comp", chunk, clients, context.cursor):
        resp = cast(CompletionResponse, client.message)
        parsed = parse(
            pc,
            extern_type=ExternLSP,
            client=client.name,
            encoding=client.offset_encoding,
            short_name=short_name,
            cursors=context.cursor,
            always_on_top=always_on_top,
            weight_adjust=weight_adjust,
            resp=resp,
        )
        yield parsed, client.peers, client.elapsed


async def comp_lsp_inline(
    short_name: str,
    always_on_top: Optional[AbstractSet[Optional[str]]],
    weight_adjust: float,
    context: Context,
    chunk: int,
    clients: AbstractSet[str],
) -> AsyncIterator[_Rsp]:
    async for client in async_request(
        "lsp_inline_comp", chunk, clients, context.cursor
    ):
        resp = cast(InLineCompletionResponse, client.message)
        parsed = parse_inline(
            filetype=context.filetype,
            extern_type=ExternLSP,
            client=client.name,
            encoding=client.offset_encoding,
            short_name=short_name,
            cursors=context.cursor,
            always_on_top=always_on_top,
            weight_adjust=weight_adjust,
            resp=resp,
        )
        yield parsed, client.peers, client.elapsed


async def comp_thirdparty(
    short_name: str,
    always_on_top: Optional[AbstractSet[Optional[str]]],
    weight_adjust: float,
    context: Context,
    chunk: int,
    clients: AbstractSet[str],
) -> AsyncIterator[LSPcomp]:
    pc = await protocol()

    async for client in async_request(
        "lsp_third_party", chunk, clients, context.cursor, context.line
    ):
        name = client.name or short_name
        resp = cast(CompletionResponse, client.message)
        parsed = parse(
            pc,
            extern_type=ExternLUA,
            client=client.name,
            encoding=client.offset_encoding,
            short_name=name,
            cursors=context.cursor,
            always_on_top=always_on_top,
            weight_adjust=weight_adjust,
            resp=resp,
        )
        yield parsed


async def comp_thirdparty_inline(
    short_name: str,
    always_on_top: Optional[AbstractSet[Optional[str]]],
    weight_adjust: float,
    context: Context,
    chunk: int,
    clients: AbstractSet[str],
) -> AsyncIterator[LSPcomp]:
    async for client in async_request(
        "lsp_inline_third_party", chunk, clients, context.cursor, context.line
    ):
        name = client.name or short_name
        resp = cast(InLineCompletionResponse, client.message)
        parsed = parse_inline(
            filetype=context.filetype,
            extern_type=ExternLSP,
            client=name,
            encoding=client.offset_encoding,
            short_name=short_name,
            cursors=context.cursor,
            always_on_top=always_on_top,
            weight_adjust=weight_adjust,
            resp=resp,
        )
        yield parsed
