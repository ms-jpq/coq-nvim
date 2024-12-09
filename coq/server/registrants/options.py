from pynvim_pp.keymap import Keymap
from pynvim_pp.nvim import Nvim
from pynvim_pp.settings import Settings
from pynvim_pp.types import NoneType

from ...registry import NAMESPACE, atomic, autocmd, rpc
from ...shared.settings import KeyMapping
from ..rt_types import Stack
from ..state import state
from .marks import nav_mark
from .omnifunc import omnifunc
from .preview import preview_preview
from .repeat import repeat
from .user_snippets import eval_snips


@rpc()
async def _update_pumheight(stack: Stack) -> None:
    height, width = await Nvim.size()
    state(screen=(width, height))

    pumheight = min(
        round(height * stack.settings.display.pum.y_ratio),
        stack.settings.display.pum.y_max_len,
    )
    await Nvim.opts.set("pumheight", val=pumheight)


atomic.exec_lua(f"{NAMESPACE}.{_update_pumheight.method}()", ())
_ = autocmd("VimResized") << f"lua {NAMESPACE}.{_update_pumheight.method}()"


async def set_options(mapping: KeyMapping, fast_close: bool) -> None:
    settings = Settings()
    keymap = Keymap()

    settings["completefunc"] = f"v:lua.{NAMESPACE}.{omnifunc.method}"

    if mapping.eval_snips:
        _ = (
            keymap.n(mapping.eval_snips)
            << f"<cmd>lua {NAMESPACE}.{eval_snips.method}(false)<cr>"
        )
        _ = (
            keymap.v(mapping.eval_snips)
            << rf"<c-\><c-n><cmd>lua {NAMESPACE}.{eval_snips.method}(true)<cr>"
        )

    # if mapping.bigger_preview:
    #     _ = (
    #         keymap.i(mapping.bigger_preview, expr=True)
    #         << f"(pumvisible() && complete_info(['mode']).mode ==# 'eval') ? {preview_preview.method}() : '{mapping.bigger_preview}'"
    #     )

    if mapping.jump_to_mark:
        _ = (
            keymap.n(mapping.jump_to_mark)
            << f"<cmd>lua {NAMESPACE}.{nav_mark.method}()<cr>"
        )
        _ = (
            keymap.iv(mapping.jump_to_mark)
            << rf"<c-\><c-n><cmd>lua {NAMESPACE}.{nav_mark.method}()<cr>"
        )

    if mapping.repeat:
        _ = keymap.n(mapping.repeat) << f"<cmd>lua {NAMESPACE}.{repeat.method}()<cr>"

    if mapping.manual_complete:
        _ = (
            keymap.i(mapping.manual_complete, expr=True)
            << "pumvisible() ? '<c-e><c-x><c-u>' : '<c-x><c-u>'"
        )
        if not mapping.manual_complete_insertion_only:
            _ = keymap.nv(mapping.manual_complete) << r"<c-\><c-n>i<c-x><c-u>"

    settings["completeopt"] += (
        "noinsert",
        "menuone",
        *(() if mapping.pre_select else ("noselect",)),
    )

    if mapping.recommended:
        _ = keymap.i("<esc>", expr=True) << "pumvisible() ? '<c-e><esc>' : '<esc>'"
        _ = keymap.i("<c-c>", expr=True) << "pumvisible() ? '<c-e><c-c>' : '<c-c>'"
        _ = keymap.i("<bs>", expr=True) << "pumvisible() ? '<c-e><bs>' : '<bs>'"
        _ = keymap.i("<c-w>", expr=True) << "pumvisible() ? '<c-e><c-w>' : '<c-w>'"
        _ = keymap.i("<c-u>", expr=True) << "pumvisible() ? '<c-e><c-u>' : '<c-u>'"
        _ = (
            keymap.i("<cr>", expr=True)
            << "pumvisible() ? (complete_info(['selected']).selected == -1 ? '<c-e><cr>' : '<c-y>') : '<cr>'"
        )
        _ = (
            keymap.i("<tab>", expr=True)
            << "pumvisible() && !empty(trim(strpart(getline('.'), 0, col('.') - 1))) ? '<c-n>' : '<tab>'"
        )
        _ = (
            keymap.i("<s-tab>", expr=True)
            << "pumvisible() && !empty(trim(strpart(getline('.'), 0, col('.') - 1))) ? '<c-p>' : '<bs>'"
        )

    if fast_close:
        settings["shortmess"] += "c"
    await (settings.drain() + keymap.drain(buf=None)).commit(NoneType)
