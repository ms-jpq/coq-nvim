from asyncio import AbstractEventLoop, Event
from asyncio import Lock as ALock
from asyncio import get_running_loop, run_coroutine_threadsafe, sleep, wrap_future
from dataclasses import dataclass, replace
from datetime import timedelta
from functools import lru_cache
from itertools import count
from threading import Lock
from time import monotonic
from typing import (
    AbstractSet,
    Any,
    AsyncIterator,
    Iterator,
    Mapping,
    MutableMapping,
    MutableSequence,
    Optional,
    Sequence,
    Tuple,
)

from pynvim_pp.logging import log
from pynvim_pp.nvim import Nvim
from pynvim_pp.types import NoneType
from std2.pickle.decoder import new_decoder

from ...registry import NAMESPACE, rpc
from ...server.rt_types import Stack
from ...shared.timeit import timeit
from ...shared.types import UTF8, UTF16, UTF32, Encoding


@dataclass(frozen=True)
class _Client:
    name: Optional[str]
    peers: AbstractSet[str]
    offset_encoding: Encoding
    elapsed: timedelta
    message: Any


@dataclass(frozen=True)
class _Session:
    uid: int
    instance: float
    done: bool
    acc: MutableSequence[Tuple[_Client, Optional[int]]]


@dataclass(frozen=True)
class _Payload:
    multipart: Optional[int]
    name: str
    method: Optional[str]
    uid: int
    offset_encoding: Optional[str]
    client: Optional[str]
    done: bool
    reply: Any
    client_names: Sequence[Optional[str]] = ()


_DECODER = new_decoder[_Payload](_Payload)
_ENCODING_MAP: Mapping[Optional[str], Encoding] = {
    "utf8": UTF8,
    "utf16": UTF16,
    "utf32": UTF32,
}

_LOCK = Lock()
_STATE: MutableMapping[str, _Session] = {}


@lru_cache(maxsize=None)
def _uids(_: str) -> Iterator[int]:
    return count()


@lru_cache(maxsize=None)
def _events(_: str) -> Tuple[AbstractEventLoop, ALock, Event]:
    loop = get_running_loop()
    return (loop, ALock(), Event())


async def _lsp_pull(
    n: int, name: str, client: Optional[str], uid: int
) -> AsyncIterator[Sequence[Any]]:
    lo = 1
    hi = n
    while True:
        part: Sequence[Any] = await Nvim.api.exec_lua(
            tuple,
            f"return {NAMESPACE}.lsp_pull(...)",
            (client, name, uid, lo, hi),
        )
        lo = hi + 1
        hi = hi + n
        length = hi - lo + 1

        assert isinstance(part, Sequence)
        yield part
        await sleep(0)
        if len(part) < length:
            break


@rpc(blocking=False)
async def _lsp_notify(stack: Stack, rpayload: _Payload) -> None:
    payload = _DECODER(rpayload)
    origin, lock, activity = _events(payload.name)

    async def cont() -> None:
        with _LOCK:
            state = _STATE.get(payload.name)
        assert state

        if payload.uid >= state.uid:
            encoding = (payload.offset_encoding or "").casefold().replace("-", "")
            offset_encoding = _ENCODING_MAP.get(encoding, UTF16)
            client = _Client(
                name=payload.client,
                peers={name for name in payload.client_names if name},
                offset_encoding=offset_encoding,
                elapsed=timedelta(seconds=monotonic() - state.instance),
                message=payload.reply,
            )
            acc = [
                *(state.acc if state and payload.uid == state.uid else ()),
                (client, payload.multipart),
            ]
            session = _Session(
                uid=payload.uid, instance=state.instance, done=payload.done, acc=acc
            )
            with _LOCK:
                _STATE[payload.name] = session

        async with lock:
            activity.set()

    if get_running_loop() == origin:
        await cont()
    else:
        f = run_coroutine_threadsafe(cont(), loop=origin)
        await wrap_future(f)


async def async_request(
    name: str, multipart: Optional[int], clients: AbstractSet[str], *args: Any
) -> AsyncIterator[_Client]:
    with timeit(f"LSP :: {name}"):
        (_, lock, activity), uid = _events(name), next(_uids(name))

        with _LOCK:
            _STATE[name] = _Session(uid=uid, instance=monotonic(), done=False, acc=[])

        # wake up previous generators
        activity.set()
        # wait for previous generators to exit
        await Nvim.api.exec_lua(
            NoneType,
            f"{NAMESPACE}.{name}(...)",
            (name, multipart, uid, tuple(clients), *args),
        )

        while True:
            with _LOCK:
                state = _STATE.get(name)
            assert state

            if state.uid == uid:
                while state.acc:
                    client, multipart = state.acc.pop()
                    if multipart:
                        async for part in _lsp_pull(
                            multipart, name=name, client=client.name, uid=uid
                        ):
                            if isinstance(
                                client.message, MutableMapping
                            ) and isinstance(client.message.get("items"), Sequence):
                                message = {**client.message, "items": part}
                                yield replace(client, message=message)
                            else:
                                yield replace(client, message=part)
                    else:
                        yield client
                if state.done:
                    with _LOCK:
                        _STATE.pop(name, None)
                    break
            elif state.uid > uid:
                break
            else:
                log.info(
                    "%s", f"<><> DELAYED LSP RESP <><> :: {name} {state.uid} {uid}"
                )
                break

            await activity.wait()

            with _LOCK:
                state = _STATE.get(name)
            if state and state.uid != uid:
                break

            # yield to all other generators to avoid blocking their exit
            async with lock:
                await sleep(0)
                activity.clear()
