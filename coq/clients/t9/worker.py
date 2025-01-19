import sys
from asyncio import (
    IncompleteReadError,
    LimitOverrunError,
    StreamReader,
    create_subprocess_exec,
    gather,
    shield,
    sleep,
)
from asyncio.locks import Lock
from asyncio.subprocess import Process
from contextlib import suppress
from itertools import chain, count
from json import dumps, loads
from json.decoder import JSONDecodeError
from pathlib import PurePath
from subprocess import DEVNULL, PIPE
from typing import Any, AsyncIterator, Iterator, Mapping, Optional, Sequence, cast

from pynvim_pp.lib import decode, encode
from pynvim_pp.logging import log, suppress_and_log
from pynvim_pp.nvim import Nvim
from std2.pickle.decoder import new_decoder
from std2.pickle.encoder import new_encoder
from std2.pickle.types import DecodeError
from std2.platform import OS, os

from ...consts import DEBUG
from ...lang import LANG
from ...lsp.protocol import LSProtocol, protocol
from ...shared.executor import AsyncExecutor, very_nice
from ...shared.runtime import Supervisor
from ...shared.runtime import Worker as BaseWorker
from ...shared.settings import T9Client
from ...shared.sql import BIGGEST_INT
from ...shared.types import Completion, Context, ContextualEdit, Doc
from .install import ensure_updated, t9_bin, x_ok
from .types import ReqL1, ReqL2, Request, RespL1, Response

if sys.platform == "win32":
    from subprocess import BELOW_NORMAL_PRIORITY_CLASS

    nice = lambda _: None
else:
    from os import nice

    BELOW_NORMAL_PRIORITY_CLASS = 0


_VERSION = "4.5.10"

_DECODER = new_decoder[RespL1](RespL1, strict=False)
_ENCODER = new_encoder[Request](Request)
_NL = b"\n"


def _encode(context: Context, id: int, limit: int) -> Any:
    row, _ = context.position
    before = context.linefeed.join(chain(context.lines_before, (context.line_before,)))
    after = context.linefeed.join(chain((context.line_after,), context.lines_after))
    ibg = row - context.win_size <= 0
    ieof = row + context.win_size >= context.line_count

    l2 = ReqL2(
        correlation_id=id,
        filename=context.filename,
        before=before,
        after=after,
        region_includes_beginning=ibg,
        region_includes_end=ieof,
        max_num_results=None if context.manual else limit,
    )
    l1 = ReqL1(Autocomplete=l2)
    req = Request(request=l1, version=_VERSION)
    return _ENCODER(req)


def _decode(
    protocol: LSProtocol,
    client: T9Client,
    ellipsis: str,
    syntax: str,
    id: int,
    reply: Response,
) -> Iterator[Completion]:
    if (
        not isinstance(reply, Mapping)
        or ((r_id := reply.get("correlation_id")) and r_id != id)
        or not isinstance((old_prefix := reply.get("old_prefix")), str)
        or not isinstance((results := reply.get("results")), Sequence)
    ):
        log.warning("%s", reply)
    else:
        for result in results:
            try:
                resp = _DECODER(result)
            except DecodeError as e:
                log.warning("%s", e)
            else:
                new_text = resp.new_prefix + resp.new_suffix
                edit = ContextualEdit(
                    old_prefix=old_prefix,
                    new_prefix=resp.new_prefix,
                    old_suffix=resp.old_suffix,
                    new_text=new_text,
                )

                pre_lines = resp.new_prefix.splitlines() or ("",)
                post_lines = resp.new_suffix.splitlines() or ("",)
                label_pre, *pre = pre_lines
                label_post, *post = post_lines
                e_pre = ellipsis if pre else ""
                e_post = ellipsis if post else ""
                label = label_pre + e_pre + label_post + e_post
                *_, s_pre = pre_lines
                s_post, *_ = post_lines
                sort_by = s_pre + s_post

                doc = Doc(text=new_text, syntax=syntax) if e_pre or e_post else None

                kind = protocol.CompletionItemKind.get(resp.kind)
                cmp = Completion(
                    source=client.short_name,
                    always_on_top=client.always_on_top,
                    weight_adjust=client.weight_adjust,
                    label=label,
                    sort_by=sort_by,
                    primary_edit=edit,
                    adjust_indent=False,
                    kind=kind or "",
                    icon_match=kind,
                    doc=doc,
                )
                yield cmp


def _nice() -> None:
    with suppress(PermissionError):
        nice(19)


async def _proc(bin: PurePath, cwd: PurePath) -> Optional[Process]:
    kwargs = {} if os is OS.windows else {"preexec_fn": _nice}
    prefix = await very_nice()
    log = (f"--log-file-path={cwd / 't9.log'}",) if DEBUG else ()
    try:
        proc = await create_subprocess_exec(
            *prefix,
            bin,
            "--client=coq.nvim",
            *log,
            stdin=PIPE,
            stdout=PIPE,
            stderr=DEVNULL,
            cwd=cwd,
            creationflags=BELOW_NORMAL_PRIORITY_CLASS,
            **kwargs,  # type: ignore
        )
    except FileNotFoundError:
        return None
    else:
        return proc


async def _readline(stdout: StreamReader) -> bytes:
    acc = bytearray()
    while True:
        try:
            b = await stdout.readuntil(_NL)
        except LimitOverrunError as e:
            c = await stdout.readexactly(e.consumed)
            acc.extend(c)
        else:
            acc.extend(b)
            break

    return acc


class Worker(BaseWorker[T9Client, None]):
    def __init__(
        self,
        ex: AsyncExecutor,
        supervisor: Supervisor,
        always_wait: bool,
        options: T9Client,
        misc: None,
    ) -> None:
        self._lock = Lock()
        self._bin: Optional[PurePath] = None
        self._proc: Optional[Process] = None
        self._cwd: Optional[PurePath] = None
        self._count = count()
        self._t9_locked = False
        super().__init__(
            ex,
            supervisor=supervisor,
            always_wait=always_wait,
            options=options,
            misc=misc,
        )
        self._ex.run(gather(self._install(), self._poll()))

    def interrupt(self) -> None:
        pass

    async def _poll(self) -> None:
        with suppress_and_log():
            try:
                while True:
                    await sleep(9)
            finally:
                proc = self._proc
                if proc:
                    with suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()

    async def _install(self) -> None:
        with suppress_and_log():
            vars_dir = self._supervisor.vars_dir / "clients" / "t9"
            if x_ok(vars_dir):
                self._bin = t9_bin(vars_dir)
            else:
                for _ in range(9):
                    await sleep(0)
                await Nvim.write(LANG("begin T9 download"))

                self._bin = await ensure_updated(
                    vars_dir,
                    retries=self._supervisor.limits.download_retries,
                    timeout=self._supervisor.limits.download_timeout,
                )

                if not self._bin:
                    await Nvim.write(LANG("failed T9 download"))
                else:
                    await Nvim.write(LANG("end T9 download"))

    async def _clean(self) -> None:
        if proc := self._proc:
            self._proc = None
            with suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()

    async def _comm(self, cwd: PurePath, json: str) -> Optional[str]:
        async def cont() -> Optional[str]:
            async with self._lock:
                if self._bin and not self._proc:
                    self._proc = await _proc(self._bin, cwd=cwd)
                    if self._proc:
                        self._cwd = cwd
                if not self._proc:
                    return None
                else:
                    assert self._proc.stdin and self._proc.stdout
                    try:
                        self._proc.stdin.write(encode(json))
                        self._proc.stdin.write(_NL)
                        await self._proc.stdin.drain()
                        out = await _readline(self._proc.stdout)
                    except (ConnectionError, IncompleteReadError) as e:
                        log.warning("%s", e)
                        await self._clean()
                        return None
                    else:
                        return decode(out)

        if self._lock.locked():
            return None
        else:
            return await shield(cont())

    async def _work(self, context: Context, timeout: float) -> AsyncIterator[Completion]:
        limit = (
            BIGGEST_INT
            if context.manual
            else self._options.max_pulls or self._supervisor.match.max_results
        )

        if self._t9_locked:
            self._t9_locked = False
            return

        async with self._work_lock:
            if self._cwd != context.cwd:
                await self._clean()

            if self._bin:
                id = next(self._count)
                req = _encode(context, id=id, limit=limit)
                json = dumps(req, check_circular=False, ensure_ascii=False)
                if reply := await self._comm(context.cwd, json=json):
                    try:
                        resp = loads(reply)
                    except JSONDecodeError as e:
                        log.warning("%s", e)
                    else:
                        if isinstance(resp, Mapping):
                            self._t9_locked = resp.get("is_locked", False)

                        pc = await protocol()
                        for comp in _decode(
                            pc,
                            client=self._options,
                            ellipsis=self._supervisor.display.pum.ellipsis,
                            syntax=context.filetype,
                            id=id,
                            reply=cast(Response, resp),
                        ):
                            yield comp
