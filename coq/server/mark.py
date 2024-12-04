from string import whitespace
from typing import Sequence
from uuid import uuid4

from pynvim_pp.buffer import Buffer, ExtMark, ExtMarker
from pynvim_pp.lib import decode
from pynvim_pp.logging import log
from pynvim_pp.nvim import Nvim
from pynvim_pp.rpc_types import NvimError

from ..lang import LANG
from ..shared.settings import Settings
from ..shared.types import Mark

NS = uuid4()

_WS = {*whitespace}


def _encode_for_display(text: str) -> str:
    encoded = "".join(
        decode(char.encode("unicode_escape")) if char in _WS else char for char in text
    )
    return encoded


async def mark(settings: Settings, buf: Buffer, marks: Sequence[Mark]) -> None:
    emarks = tuple(
        ExtMark(
            buf=buf,
            marker=ExtMarker(mark.idx + 1),
            begin=mark.begin,
            end=mark.end,
            meta={"hl_group": settings.display.mark_highlight_group},
        )
        for mark in marks
    )
    ns = await Nvim.create_namespace(NS)
    await buf.clear_namespace(ns)

    try:
        await buf.set_extmarks(ns, extmarks=emarks)
    except NvimError:
        log.warning("%s", f"bad mark locations {marks}")
    else:
        regions = _encode_for_display(" ".join(f"[{mark.text}]" for mark in marks))
        msg = LANG("added marks", regions=regions)
        await Nvim.write(msg)
