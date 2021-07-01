from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Optional

from pynvim import Nvim
from std2.pickle import new_encoder


class VimCompKind(Enum):
    variable = "v"
    function = "f"
    member = "m"
    typedef = "t"
    define = "d"


@dataclass(frozen=True)
class VimCompletion:
    word: str
    abbr: Optional[str] = None
    menu: Optional[str] = None
    info: Optional[str] = None
    kind: Optional[str] = None
    icase: Optional[int] = None
    equal: Optional[int] = None
    dup: Optional[int] = None
    empty: Optional[int] = None
    user_data: Optional[Any] = None


_LUA = """
(function(col, items)
  vim.schedule(function()
    local mode = vim.api.nvim_get_mode().mode
    if mode == "i" or mode == "ic" then
      vim.fn.complete(col, items)
    end
  end)
end)(...)
"""

_ENCODER = new_encoder(VimCompletion)


def complete(nvim: Nvim, col: int, comp: Iterable[VimCompletion]) -> None:
    serialized = tuple(
        {k: v for k, v in _ENCODER(cmp).items() if v is not None} for cmp in comp
    )

    nvim.api.exec_lua(_LUA, (col + 1, serialized))

