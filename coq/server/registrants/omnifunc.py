from asyncio import create_task, gather, sleep
from dataclasses import replace
from time import monotonic
from typing import AbstractSet, Any, Literal, Mapping, Optional, Sequence, Union
from uuid import UUID, uuid4

from pynvim_pp.buffer import Buffer, ExtMark, ExtMarker
from pynvim_pp.lib import encode
from pynvim_pp.logging import log, suppress_and_log
from pynvim_pp.nvim import Nvim
from std2.locale import si_prefixed_smol
from std2.pickle.decoder import new_decoder
from std2.pickle.types import DecodeError

from ...consts import DEBUG
from ...lsp.requests.command import cmd
from ...lsp.requests.resolve import resolve
from ...registry import NAMESPACE, autocmd, rpc
from ...shared.aio import with_timeout
from ...shared.runtime import Metric
from ...shared.types import ChangeEvent, Context, ExternLSP, ExternPath
from ..completions import complete
from ..context import context
from ..edit import NS, edit
from ..rt_types import Stack
from ..state import State, state
from ..trans import trans

_UDECODER = new_decoder[UUID](UUID)


def _should_cont(
    state: State, prev: Context, cur: Context, skip_after: AbstractSet[str]
) -> bool:
    if cur.manual:
        return True
    elif prev.change_id == cur.change_id:
        return False
    elif cur.position == state.inserted_pos:
        if isinstance(extern := state.last_edit.comp.extern, ExternPath):
            return extern.is_dir
        else:
            return False
    elif any(cur.line_before.endswith(token) for token in skip_after):
        return False

    return True


async def comp_func(
    stack: Stack, s: State, change: Optional[ChangeEvent], t0: float, manual: bool
) -> None:
    with suppress_and_log():
        ctx = await context(
            options=stack.settings.match, state=s, change=change, manual=manual
        )
        should = (
            _should_cont(
                s,
                prev=s.context,
                cur=ctx,
                skip_after=stack.settings.completion.skip_after,
            )
            if ctx
            else False
        )
        _, col = ctx.position

        if should:
            state(context=ctx)
            metrics, _ = await gather(
                stack.supervisor.collect(ctx),
                (
                    complete(stack=stack, col=col, comps=())
                    if stack.settings.display.pum.fast_close
                    else sleep(0)
                ),
            )
            s = state()
            if s.change_id == ctx.change_id:
                vim_comps = tuple(
                    trans(
                        stack,
                        pum_width=s.pum_width,
                        context=ctx,
                        metrics=metrics,
                    )
                )
                await complete(stack=stack, col=col, comps=vim_comps)
                if DEBUG:
                    t1 = monotonic()
                    delta = t1 - t0
                    msg = f"TOTAL >>> {si_prefixed_smol(delta, precision=0)}s".ljust(8)
                    log.info("%s", msg)
        else:
            await complete(stack=stack, col=col, comps=())
            state(inserted_pos=(-1, -1))


@rpc()
async def omnifunc(
    stack: Stack, findstart: Literal[0, 1], _base: str
) -> Union[int, Sequence[Mapping[str, Any]]]:
    t0 = monotonic()
    if findstart:
        return -1
    else:
        s = state(commit_id=uuid4())
        create_task(comp_func(stack=stack, manual=True, change=None, t0=t0, s=s))
        return ()


async def _resolve(stack: Stack, metric: Metric) -> Metric:
    if comp := stack.lru.get(metric.comp.uid):
        return replace(
            metric,
            comp=replace(metric.comp, secondary_edits=comp.secondary_edits),
        )
    elif not isinstance((extern := metric.comp.extern), ExternLSP) or extern.inline:
        return metric
    elif comp := await with_timeout(
        stack.settings.clients.lsp.resolve_timeout, co=resolve(extern=extern)
    ):
        return replace(
            metric,
            comp=replace(metric.comp, secondary_edits=comp.secondary_edits),
        )

    else:
        return metric


@rpc()
async def _comp_done(stack: Stack, event: Mapping[str, Any]) -> None:
    if data := event.get("user_data"):
        try:
            uid = _UDECODER(data)
        except DecodeError:
            pass
        else:
            s = state()
            if (metric := stack.metrics.get(uid)) and (ctx := s.context):
                row, col = s.context.position
                buf = await Buffer.get_current()
                if ctx.buf_id == buf.number:
                    ns = await Nvim.create_namespace(NS)
                    await buf.clear_namespace(ns)
                    before, *_ = await buf.get_lines(lo=row, hi=row + 1)

                    e1 = ExtMark(
                        buf=buf,
                        marker=ExtMarker(1),
                        begin=(row, 0),
                        end=(row, col),
                        meta={},
                    )
                    e2 = ExtMark(
                        buf=buf,
                        marker=ExtMarker(2),
                        begin=(row, col),
                        end=(row, len(encode(before))),
                        meta={},
                    )
                    await buf.set_extmarks(ns, extmarks=(e1, e2))
                    new_metric = await _resolve(stack=stack, metric=metric)

                    if isinstance((extern := new_metric.comp.extern), ExternLSP):
                        await with_timeout(
                            stack.settings.clients.lsp.resolve_timeout,
                            co=cmd(extern=extern),
                        )

                    if new_metric.comp.uid in stack.metrics:
                        if inserted := await edit(
                            stack=stack,
                            state=s,
                            metric=new_metric,
                            synthetic=False,
                        ):
                            inserted_at, text_trans = inserted
                        else:
                            inserted_at, text_trans = (-1, -1), None

                        state(
                            inserted_pos=inserted_at,
                            text_trans=text_trans,
                            last_edit=new_metric,
                            commit_id=uuid4(),
                        )
                    else:
                        log.warning("%s", "delayed completion")


_ = (
    autocmd("CompleteDone")
    << f"lua {NAMESPACE}.{_comp_done.method}(vim.v.completed_item)"
)
