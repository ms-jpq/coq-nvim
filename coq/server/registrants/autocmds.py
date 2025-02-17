from asyncio import create_task, gather, sleep
from contextlib import suppress
from uuid import uuid4

from pynvim_pp.buffer import Buffer
from pynvim_pp.float_win import list_floatwins
from pynvim_pp.nvim import Nvim
from pynvim_pp.rpc_types import NvimError
from pynvim_pp.settings import Settings
from pynvim_pp.types import NoneType
from std2.asyncio import Cancellation
from std2.locale import si_prefixed_smol

from ...clients.buffers.worker import Worker as BufWorker
from ...clients.registers.worker import Worker as RegWorker
from ...clients.tags.worker import Worker as TagsWorker
from ...clients.tmux.worker import Worker as TmuxWorker
from ...clients.tree_sitter.worker import Worker as TSWorker
from ...lang import LANG
from ...registry import NAMESPACE, atomic, autocmd, rpc
from ..context import context
from ..rt_types import Stack
from ..state import state
from .omnifunc import omnifunc

_NS = uuid4()
_die = Cancellation()


@rpc()
async def _kill_float_wins(stack: Stack) -> None:
    wins = [w async for w in list_floatwins(_NS)]
    if len(wins) != 2:
        for win in wins:
            await win.close()


_ = autocmd("WinEnter") << f"lua {NAMESPACE}.{_kill_float_wins.method}()"


@rpc()
async def _new_cwd(stack: Stack) -> None:
    cwd = await Nvim.getcwd()
    s = state(cwd=cwd)
    for worker in stack.workers:
        if isinstance(worker, TagsWorker):
            create_task(worker.swap(s.cwd))
            break


_ = autocmd("DirChanged") << f"lua {NAMESPACE}.{_new_cwd.method}()"


@rpc()
async def _set_complete_func(stack: Stack) -> None:
    with suppress(NvimError):
        settings = Settings()
        settings["completefunc"] = f"v:lua.{NAMESPACE}.{omnifunc.method}"
        await settings.drain().commit(NoneType)


_ = (
    autocmd("FileType", "CursorHold", "CursorHoldI", "InsertEnter")
    << f"lua {NAMESPACE}.{_set_complete_func.method}()"
)


@rpc()
async def _ft_changed(stack: Stack) -> None:
    for worker in stack.workers:
        if isinstance(worker, BufWorker):
            buf = await Buffer.get_current()
            ft = await buf.filetype()
            filename = await buf.get_name() or ""
            create_task(worker.buf_update(buf.number, filetype=ft, filename=filename))
            break


_ = autocmd("FileType") << f"lua {NAMESPACE}.{_ft_changed.method}()"
atomic.exec_lua(f"{NAMESPACE}.{_ft_changed.method}()", ())


@rpc()
async def _insert_enter(stack: Stack) -> None:
    for worker in stack.workers:
        if isinstance(worker, TSWorker):
            buf = await Buffer.get_current()
            nono_bufs = state().nono_bufs
            if buf.number not in nono_bufs:

                async def cont() -> None:
                    if populated := await worker.populate():
                        keep_going, elapsed = populated

                        if not keep_going:
                            state(nono_bufs={buf.number})
                            msg = LANG(
                                "source slow",
                                source=stack.settings.clients.tree_sitter.short_name,
                                elapsed=si_prefixed_smol(elapsed, precision=0),
                            )
                            await Nvim.write(msg, error=True)
                    create_task(cont())

            break


@rpc()
async def _on_focus(stack: Stack) -> None:
    for worker in stack.workers:
        if isinstance(worker, TmuxWorker):
            create_task(worker.periodical())
            break


_ = autocmd("FocusGained") << f"lua {NAMESPACE}.{_on_focus.method}()"


@rpc()
async def _when_idle(stack: Stack) -> None:
    @_die
    async def cont() -> None:
        await sleep(stack.settings.limits.idle_timeout)
        with suppress(NvimError):
            buf = await Buffer.get_current()
            buf_type = await buf.opts.get(str, "buftype")
            if buf_type == "terminal":
                await Nvim.api.buf_detach(NoneType, buf)
                state(nono_bufs={buf.number})

        async def idle() -> None:
            s = state()
            try:
                ctx = await context(
                    options=stack.settings.match, state=s, change=None, manual=False
                )
            except NvimError:
                ctx = None
            await stack.supervisor.notify_idle(ctx)

        await gather(_insert_enter(stack=stack), idle())

    create_task(cont())


_ = autocmd("CursorHold", "CursorHoldI") << f"lua {NAMESPACE}.{_when_idle.method}()"


@rpc()
async def _on_yank(stack: Stack, regsize: int, operator: str, regname: str) -> None:
    if operator == "y":
        for worker in stack.workers:
            if isinstance(worker, RegWorker):
                await worker.post_yank(regname, regsize=regsize)


_LUA = f"""
(function()
  local acc = 0
  for _, line in pairs(vim.v.event.regcontents) do
    acc = acc + #line
  end
  {NAMESPACE}.{_on_yank.method}(acc, vim.v.event.operator, vim.v.event.regname)
end)()
"""

_ = autocmd("TextYankPost") << f"lua {_LUA}"
