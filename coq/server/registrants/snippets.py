from asyncio import gather
from asyncio.tasks import as_completed
from contextlib import suppress
from dataclasses import dataclass
from itertools import chain
from json import JSONDecodeError, dumps, loads
from math import inf
from os.path import expanduser, expandvars
from pathlib import Path, PurePath
from posixpath import normcase
from string import Template
from tempfile import NamedTemporaryFile
from textwrap import dedent
from typing import (
    AbstractSet,
    Any,
    AsyncIterator,
    Iterable,
    Iterator,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from pynvim_pp.lib import decode
from pynvim_pp.logging import log
from pynvim_pp.nvim import Nvim
from pynvim_pp.preview import set_preview
from std2.asyncio import to_thread
from std2.graphlib import recur_sort
from std2.pathlib import walk
from std2.pickle.decoder import new_decoder
from std2.pickle.encoder import new_encoder
from std2.pickle.types import DecodeError

from ...clients.snippet.worker import Worker as SnipWorker
from ...lang import LANG
from ...paths.show import fmt_path
from ...registry import NAMESPACE, atomic, rpc
from ...shared.context import EMPTY_CONTEXT
from ...shared.settings import CompleteOptions, MatchOptions, SnippetWarnings
from ...shared.timeit import timeit
from ...shared.types import (
    UTF8,
    Edit,
    Mark,
    SnippetEdit,
    SnippetGrammar,
    TextTransforms,
)
from ...snippets.loaders.load import load_direct
from ...snippets.loaders.neosnippet import load_neosnippet
from ...snippets.parse import parse_basic
from ...snippets.parsers.types import ParseError, ParseInfo
from ...snippets.types import SCHEMA, LoadedSnips, LoadError, ParsedSnippet
from ..rt_types import Stack

BUNDLED_PATH_TPL = Template("coq+snippets+${schema}.json")
_USER_PATH_TPL = Template("users+${schema}.json")
_SUB_PATH = PurePath("clients", "snippets")


@dataclass(frozen=True)
class Compiled:
    path: PurePath
    filetype: str
    exts: AbstractSet[str]
    parsed: Sequence[Tuple[ParsedSnippet, Edit, Sequence[Mark], TextTransforms]]


async def _bundled_mtimes() -> Mapping[Path, float]:
    rtp = await Nvim.list_runtime_paths()

    def c1() -> Iterator[Tuple[Path, float]]:
        for path in rtp:
            json = path / BUNDLED_PATH_TPL.substitute(schema=SCHEMA)
            with suppress(OSError):
                mtime = json.stat().st_mtime
                yield json, mtime

    return {p: m for p, m in await to_thread(lambda: tuple(c1()))}


def _resolve(stdp: Path, path: Path) -> Optional[Path]:
    if path.is_absolute():
        if path.exists():
            return path
        else:
            u_p = Path(expanduser(path))
            if u_p != path and u_p.exists():
                return u_p
            else:
                v_p = Path(expandvars(path))
                if v_p != path and v_p.exists():
                    return v_p
                else:
                    return None
    else:
        if normcase(path).startswith("~"):
            return _resolve(stdp, path=Path(expanduser(path)))
        else:
            return _resolve(stdp, path=stdp / path)


async def snippet_paths(user_path: Optional[Path]) -> Sequence[Path]:
    async def cont() -> AsyncIterator[Path]:
        if user_path:
            std_conf = Path(await Nvim.fn.stdpath(str, "config"))
            if resolved := _resolve(std_conf, path=user_path):
                yield resolved
        for path in await Nvim.list_runtime_paths():
            yield path / "coq-user-snippets"

    paths = [p async for p in cont()]
    return paths


async def user_mtimes(
    user_path: Optional[Path],
) -> Tuple[Sequence[Path], Mapping[Path, float]]:
    paths = await snippet_paths(user_path=user_path)

    def cont() -> Iterator[Tuple[Path, float]]:
        for path in paths:
            with suppress(OSError):
                for p in walk(path):
                    if p.suffix in {".snip"}:
                        mtime = p.stat().st_mtime
                        yield p, mtime

    return paths, {p: m for p, m in await to_thread(lambda: tuple(cont()))}


def _paths(vars_dir: Path) -> Tuple[Path, Path]:
    compiled = vars_dir / _SUB_PATH / _USER_PATH_TPL.substitute(schema=SCHEMA)
    meta = vars_dir / _SUB_PATH / "meta.json"
    return compiled, meta


async def _load_compiled(path: Path, mtime: float) -> Tuple[Path, float, LoadedSnips]:
    decoder = new_decoder[LoadedSnips](LoadedSnips)

    def cont() -> LoadedSnips:
        raw = decode(path.read_bytes())
        json = loads(raw)
        loaded = decoder(json)
        return loaded

    return path, mtime, await to_thread(cont)


async def _load_user_compiled(
    vars_dir: Path,
) -> Tuple[Mapping[Path, float], Mapping[Path, float]]:
    compiled, meta = _paths(vars_dir)

    def cont() -> Tuple[Mapping[Path, float], Mapping[Path, float]]:
        m1: Mapping[Path, float] = {}
        m2: Mapping[Path, float] = {}
        with suppress(OSError):
            mtime = compiled.stat().st_mtime
            m1 = {compiled: mtime}

        with suppress(OSError):
            raw = decode(meta.read_bytes())
            try:
                json = loads(raw)
                m2 = new_decoder[Mapping[Path, float]](Mapping[Path, float])(json)
            except (JSONDecodeError, DecodeError):
                meta.unlink(missing_ok=True)

        return m1, m2

    return await to_thread(cont)


def jsonify(o: Any) -> str:
    json = dumps(recur_sort(o), check_circular=False, ensure_ascii=False, indent=2)
    return json


async def _dump_compiled(
    vars_dir: Path, mtimes: Mapping[Path, float], loaded: LoadedSnips
) -> None:
    m_json = jsonify(new_encoder[Mapping[Path, float]](Mapping[Path, float])(mtimes))
    s_json = jsonify(new_encoder[LoadedSnips](LoadedSnips)(loaded))

    compiled, meta = _paths(vars_dir)
    for path, json in ((compiled, s_json), (meta, m_json)):
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            dir=path.parent, mode="w", encoding=UTF8, delete=False
        ) as fd:
            fd.write(json)
        Path(fd.name).replace(path)


def _trans(
    match: MatchOptions,
    comp: CompleteOptions,
    info: ParseInfo,
    snips: Iterable[ParsedSnippet],
) -> Iterator[Tuple[ParsedSnippet, Edit, Sequence[Mark], TextTransforms]]:
    for snip in snips:
        edit = SnippetEdit(grammar=snip.grammar, new_text=snip.content)
        parsed, marks, text_trans = parse_basic(
            match,
            comp=comp,
            adjust_indent=False,
            context=EMPTY_CONTEXT,
            snippet=edit,
            info=info,
        )
        yield snip, parsed, marks, text_trans


async def _rolling_load(
    worker: SnipWorker, cwd: PurePath, compiled: Mapping[Path, float], silent: bool
) -> None:
    for fut in as_completed(
        tuple(_load_compiled(path, mtime) for path, mtime in compiled.items())
    ):
        try:
            path, mtime, loaded = await fut
        except (OSError, JSONDecodeError, DecodeError) as e:
            tpl = """
                Failed to load compiled snips
                ${e}
                """.rstrip()
            log.warning("%s", Template(dedent(tpl)).substitute(e=type(e)))
        else:
            await worker.populate(path, mtime=mtime, loaded=loaded)
            if not silent:
                await Nvim.write(
                    LANG(
                        "fs snip load succ", path=fmt_path(cwd, path=path, is_dir=False)
                    )
                )


async def slurp_compiled(
    stack: Stack, warn: AbstractSet[SnippetWarnings], silent: bool
) -> Mapping[Path, float]:
    for worker in stack.workers:
        if isinstance(worker, SnipWorker):
            break
    else:
        return {}

    with timeit("LOAD SNIPS"):
        (
            cwd,
            bundled,
            (user_compiled, user_compiled_mtimes),
            (_, user_snips_mtimes),
            db_mtimes,
        ) = await gather(
            Nvim.getcwd(),
            _bundled_mtimes(),
            _load_user_compiled(stack.supervisor.vars_dir),
            user_mtimes(user_path=stack.settings.clients.snippets.user_path),
            worker.db_mtimes(),
        )

        if stale := db_mtimes.keys() - (bundled.keys() | user_compiled.keys()):
            await worker.clean(stale)

        if needs_loading := {
            path: mtime
            for path, mtime in chain(bundled.items(), user_compiled.items())
            if mtime > db_mtimes.get(path, -inf)
        }:
            await _rolling_load(worker, cwd=cwd, compiled=needs_loading, silent=silent)

        needs_compilation = {
            path: mtime
            for path, mtime in user_snips_mtimes.items()
            if mtime > user_compiled_mtimes.get(path, -inf)
        }

        if SnippetWarnings.missing in warn and not (bundled or user_compiled):
            await Nvim.write(LANG("fs snip load empty"))

        return needs_compilation


@rpc()
async def _load_snips(stack: Stack) -> None:
    for worker in stack.workers:
        if isinstance(worker, SnipWorker):
            try:
                needs_compilation = await slurp_compiled(
                    stack=stack,
                    warn=stack.settings.clients.snippets.warn,
                    silent=False,
                )
                if needs_compilation:
                    await compile_user_snippets(stack)
                    await slurp_compiled(stack, warn=frozenset(), silent=False)
            except (LoadError, ParseError) as e:
                preview = str(e).splitlines()
                await set_preview(syntax="", preview=preview)
                await Nvim.write(LANG("snip parse fail"))
            break


atomic.exec_lua(f"{NAMESPACE}.{_load_snips.method}()", ())


def compile_one(
    stack: Stack,
    grammar: SnippetGrammar,
    path: PurePath,
    info: ParseInfo,
    lines: Iterable[Tuple[int, str]],
) -> Compiled:
    filetype, exts, snips = load_neosnippet(grammar, path=path, lines=lines)
    parsed = tuple(
        _trans(
            stack.settings.match,
            comp=stack.settings.completion,
            info=info,
            snips=snips,
        )
    )

    compiled = Compiled(
        path=path,
        filetype=filetype,
        exts=exts,
        parsed=parsed,
    )
    return compiled


async def compile_user_snippets(stack: Stack) -> None:
    with timeit("COMPILE SNIPS"):
        info = ParseInfo(visual="", clipboard="", comment_str=("", ""))
        _, mtimes = await user_mtimes(
            user_path=stack.settings.clients.snippets.user_path
        )
        loaded = await to_thread(
            lambda: load_direct(
                lambda x: x,
                ignore_error=False,
                lsp=(),
                neosnippet=mtimes,
                ultisnip=(),
                neosnippet_grammar=SnippetGrammar.lsp,
            )
        )
        _ = tuple(
            _trans(
                stack.settings.match,
                comp=stack.settings.completion,
                info=info,
                snips=loaded.snippets.values(),
            )
        )
        try:
            await _dump_compiled(
                stack.supervisor.vars_dir, mtimes=mtimes, loaded=loaded
            )
        except OSError as e:
            await Nvim.write(e)
