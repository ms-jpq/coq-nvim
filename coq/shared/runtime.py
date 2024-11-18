from __future__ import annotations

from abc import abstractmethod
from asyncio import (
    Condition,
    Future,
    Task,
    as_completed,
    create_task,
    gather,
    run_coroutine_threadsafe,
    wait,
    wrap_future,
)
from asyncio.exceptions import CancelledError
from asyncio.tasks import FIRST_COMPLETED
from collections import deque
from concurrent.futures import Future as CFuture
from concurrent.futures import InvalidStateError, ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Coroutine,
    Deque,
    Generic,
    Iterator,
    MutableSequence,
    Optional,
    Protocol,
    Sequence,
    TypeVar,
)
from uuid import UUID, uuid4
from weakref import WeakSet

from pynvim_pp.logging import suppress_and_log
from std2.aitertools import aenumerate
from std2.asyncio import cancel

from .executor import AsyncExecutor
from .settings import (
    BaseClient,
    CompleteOptions,
    Display,
    Limits,
    MatchOptions,
    Weights,
)
from .timeit import TracingLocker, timeit
from .types import Completion, Context, Interruptible

_T = TypeVar("_T")
_T_co = TypeVar("_T_co", contravariant=True)
_O_co = TypeVar("_O_co", contravariant=True, bound=BaseClient)


@dataclass(frozen=True)
class Metric:
    instance: UUID
    comp: Completion
    weight_adjust: float
    weight: Weights
    label_width: int
    kind_width: int


class PReviewer(Protocol[_T]):
    def s_register(self, assoc: BaseClient) -> None: ...

    def begin(self, context: Context) -> _T: ...

    async def s_begin(self, token: _T, assoc: BaseClient, instance: UUID) -> None: ...

    def trans(self, token: _T, instance: UUID, completion: Completion) -> Metric: ...

    async def s_end(
        self, instance: UUID, interrupted: bool, elapsed: float, items: int
    ) -> None: ...


class Supervisor:
    def __init__(
        self,
        th: ThreadPoolExecutor,
        vars_dir: Path,
        display: Display,
        match: MatchOptions,
        comp: CompleteOptions,
        limits: Limits,
        reviewer: PReviewer,
    ) -> None:
        self.current_context: Optional[Context] = None
        self.vars_dir = vars_dir
        self.match, self.display = match, display
        self.comp, self.limits = comp, limits
        self._reviewer = reviewer

        self.threadpool = th
        self._thread_lock = Lock()
        self._workers: WeakSet[Worker] = WeakSet()

        self._lock = TracingLocker(name="Supervisor", force=True)
        self._work_task: Optional[Task] = None

    def register(self, worker: Worker, assoc: BaseClient) -> None:
        with suppress_and_log():
            self._reviewer.s_register(assoc)
            with self._thread_lock:
                self._workers.add(worker)

    async def notify_idle(self, context: Optional[Context]) -> None:
        self.current_context = context
        await gather(*(worker.idle() for worker in self._workers))

    async def interrupt(self) -> None:
        task = self._work_task
        self._work_task = None
        for worker in self._workers:
            worker.interrupt()
        if task:
            await cancel(task)

    def collect(self, context: Context) -> Awaitable[Sequence[Metric]]:
        self.current_context = context
        now = monotonic()
        timeout = (
            self.limits.completion_manual_timeout
            if context.manual
            else self.limits.completion_auto_timeout
        )

        async def cont(prev: Optional[Task]) -> Sequence[Metric]:
            with timeit("CANCEL -- ALL"):
                if prev:
                    await cancel(prev)

            with suppress_and_log(), timeit("COLLECTED -- ALL"):
                async with self._lock:
                    acc: Deque[Metric] = deque()

                    token = self._reviewer.begin(context)
                    tasks = {
                        worker.supervised(
                            context, token=token, now=now, acc=acc
                        ): worker._options.always_wait
                        for worker in self._workers
                    }
                    waiting = sum(tasks.values())

                    done, pending = await wait(tuple(tasks), timeout=timeout)
                    for fut in done:
                        waiting -= tasks.get(fut) or 0
                    if not acc or waiting:
                        for fut in as_completed(pending):
                            await fut
                            waiting -= tasks.get(fut) or 0
                            if acc and not waiting:
                                break

                    await cancel(*pending)
                    return acc

        self._work_task = task = create_task(cont(self._work_task))
        return task


class Worker(Interruptible, Generic[_O_co, _T_co]):
    @classmethod
    def init(
        cls, supervisor: Supervisor, always_wait: bool, options: _O_co, misc: _T_co
    ) -> Worker[_O_co, _T_co]:
        ex = AsyncExecutor(supervisor.threadpool)
        fut = ex.fsubmit(
            lambda: cls(
                ex,
                supervisor=supervisor,
                always_wait=always_wait,
                options=options,
                misc=misc,
            )
        )
        self: Worker[_O_co, _T_co] = fut.result()
        return self

    def __init__(
        self,
        ex: AsyncExecutor,
        supervisor: Supervisor,
        always_wait: bool,
        options: _O_co,
        misc: _T_co,
    ) -> None:
        self._ex = ex
        self._work_lock = TracingLocker(name=options.short_name, force=True)
        self._supervisor, self._options = supervisor, options
        self.always_wait = always_wait
        self._work_fut: Optional[CFuture] = None
        self._idle = Condition()
        self._interrupt_lock = Lock()
        self._interrupt_fut: CFuture = CFuture()
        self._interrupt_token = ()
        self._supervisor.register(self, assoc=options)

    @contextmanager
    def _interrupt(self) -> Iterator[None]:
        with self._interrupt_lock:
            with suppress(InvalidStateError):
                self._interrupt_fut.set_result(None)
            self._interrupt_fut = CFuture()
            yield

    async def _with_interrupt(self, co: Coroutine) -> None:
        with self._interrupt_lock:
            f = self._interrupt_fut
        fut = wrap_future(f)
        task = create_task(co)
        done, _ = await wait((task, fut), return_when=FIRST_COMPLETED)
        if fut in done:
            await cancel(task)

    @abstractmethod
    def _work(self, context: Context) -> AsyncIterator[Completion]: ...

    async def idle(self) -> None:
        async def cont() -> None:
            async with self._idle:
                self._idle.notify_all()

        await self._ex.submit(cont())

    def supervised(
        self,
        context: Context,
        token: Any,
        now: float,
        acc: MutableSequence[Metric],
    ) -> Future:
        prev = self._work_fut

        async def cont() -> None:
            instance, items = uuid4(), 0
            interrupted = False

            with timeit(f"CANCEL WORKER -- {self._options.short_name}"):
                if prev:
                    await cancel(wrap_future(prev))

            with suppress_and_log(), timeit(f"WORKER -- {self._options.short_name}"):
                await self._supervisor._reviewer.s_begin(
                    token, assoc=self._options, instance=instance
                )
                try:
                    async for items, completion in aenumerate(
                        self._work(context), start=1
                    ):
                        metric = self._supervisor._reviewer.trans(
                            token, instance=instance, completion=completion
                        )
                        acc.append(metric)
                except CancelledError:
                    interrupted = True
                    raise
                finally:
                    elapsed = monotonic() - now
                    await self._supervisor._reviewer.s_end(
                        instance,
                        interrupted=interrupted,
                        elapsed=elapsed,
                        items=items,
                    )

        self.interrupt()
        f = run_coroutine_threadsafe(cont(), self._ex.loop)
        self._work_fut = f
        fut = wrap_future(f)
        return fut
