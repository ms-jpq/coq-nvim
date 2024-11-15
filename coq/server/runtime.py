from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePath
from shutil import which
from typing import Any, Iterator, Mapping, cast

from pynvim_pp.lib import decode
from pynvim_pp.nvim import Nvim
from pynvim_pp.types import NoneType
from std2.configparser import hydrate
from std2.graphlib import merge
from std2.pickle.decoder import new_decoder
from yaml import safe_load

from ..clients.buffers.worker import Worker as BuffersWorker
from ..clients.inline.worker import Worker as InLineWorker
from ..clients.inline_third_party.worker import Worker as InLineThirdPartyWorker
from ..clients.lsp.worker import Worker as LspWorker
from ..clients.paths.worker import Worker as PathsWorker
from ..clients.registers.worker import Worker as RegistersWorker
from ..clients.snippet.worker import Worker as SnippetWorker
from ..clients.t9.worker import Worker as T9Worker
from ..clients.tags.worker import Worker as TagsWorker
from ..clients.third_party.worker import Worker as ThirdPartyWorker
from ..clients.tmux.worker import Worker as TmuxWorker
from ..clients.tree_sitter.worker import Worker as TreeWorker
from ..consts import CONFIG_YML, SETTINGS_VAR, VARS
from ..databases.insertions.database import IDB
from ..shared.lru import LRU
from ..shared.runtime import Supervisor, Worker
from ..shared.settings import LSPClient, Settings
from .reviewer import Reviewer
from .rt_types import Stack, ValidationError
from .state import state


async def _settings() -> Settings:
    yml = safe_load(decode(CONFIG_YML.read_bytes()))
    user_config = cast(Any, (await Nvim.vars.get(NoneType, SETTINGS_VAR)) or {})
    u_conf = hydrate(user_config)

    if isinstance(u_conf, Mapping):
        if isinstance(display := u_conf.get("display"), Mapping):
            if isinstance(icons := display.get("icons"), Mapping):
                yml_icons = yml["display"]["icons"]
                if (aliases := icons.get("aliases")) is not None:
                    yml_icons["aliases"] = aliases
                if (mappings := icons.get("mappings")) is not None:
                    yml_icons["mappings"] = mappings

    merged = merge(yml, u_conf, replace=True)
    config = new_decoder[Settings](Settings)(merged)

    if config.match.max_results <= 0:
        raise ValidationError("match.max_results <= 0")

    return config


def _from_each_according_to_their_ability(
    settings: Settings,
    vars_dir: Path,
    cwd: PurePath,
    supervisor: Supervisor,
) -> Iterator[Worker]:
    clients = settings.clients

    if clients.buffers.enabled:
        yield BuffersWorker.init(
            supervisor,
            always_wait=clients.buffers.always_wait,
            options=clients.buffers,
            misc=None,
        )

    if clients.paths.enabled:
        yield PathsWorker.init(
            supervisor,
            always_wait=clients.paths.always_wait,
            options=clients.paths,
            misc=None,
        )

    if clients.tree_sitter.enabled:
        yield TreeWorker.init(
            supervisor,
            always_wait=clients.tree_sitter.always_wait,
            options=clients.tree_sitter,
            misc=None,
        )

    if clients.lsp.enabled:
        yield LspWorker.init(
            supervisor,
            always_wait=clients.lsp.always_wait,
            options=clients.lsp,
            misc=None,
        )

    if clients.lsp_inline.enabled:
        yield InLineWorker.init(
            supervisor,
            always_wait=clients.lsp_inline.always_wait,
            options=clients.lsp_inline,
            misc=None,
        )

    if clients.registers.enabled:
        yield RegistersWorker.init(
            supervisor,
            always_wait=clients.registers.always_wait,
            options=clients.registers,
            misc=None,
        )

    if clients.third_party.enabled:
        yield ThirdPartyWorker.init(
            supervisor,
            always_wait=clients.third_party.always_wait,
            options=cast(LSPClient, clients.third_party),
            misc=None,
        )

    if clients.third_party_inline.enabled:
        yield InLineThirdPartyWorker.init(
            supervisor,
            always_wait=clients.third_party_inline.always_wait,
            options=cast(LSPClient, clients.third_party_inline),
            misc=None,
        )

    if clients.snippets.enabled:
        yield SnippetWorker.init(
            supervisor,
            always_wait=clients.snippets.always_wait,
            options=clients.snippets,
            misc=vars_dir,
        )

    if clients.tags.enabled and (ctags := which("ctags")):
        yield TagsWorker.init(
            supervisor,
            always_wait=clients.tags.always_wait,
            options=clients.tags,
            misc=(Path(ctags), vars_dir, cwd),
        )

    if clients.tmux.enabled and (tmux := which("tmux")):
        yield TmuxWorker.init(
            supervisor,
            always_wait=clients.tmux.always_wait,
            options=clients.tmux,
            misc=Path(tmux),
        )

    if clients.tabnine.enabled:
        yield T9Worker.init(
            supervisor,
            always_wait=clients.tabnine.always_wait,
            options=clients.tabnine,
            misc=None,
        )


async def stack(th: ThreadPoolExecutor) -> Stack:
    settings = await _settings()
    pum_width = await Nvim.opts.get(int, "pumwidth")
    vars_dir = (
        Path(await Nvim.fn.stdpath(str, "cache")) / "coq" if settings.xdg else VARS
    )
    s = state(cwd=await Nvim.getcwd(), pum_width=pum_width)
    idb = IDB()
    reviewer = Reviewer(
        icons=settings.display.icons,
        options=settings.match,
        db=idb,
    )
    supervisor = Supervisor(
        th=th,
        vars_dir=vars_dir,
        display=settings.display,
        match=settings.match,
        comp=settings.completion,
        limits=settings.limits,
        reviewer=reviewer,
    )
    workers = {
        *_from_each_according_to_their_ability(
            settings,
            vars_dir=vars_dir,
            cwd=s.cwd,
            supervisor=supervisor,
        )
    }
    stack = Stack(
        settings=settings,
        lru=LRU(size=settings.match.max_results),
        metrics={},
        idb=idb,
        supervisor=supervisor,
        workers=workers,
    )
    return stack
