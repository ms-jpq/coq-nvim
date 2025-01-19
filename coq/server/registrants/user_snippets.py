from argparse import Namespace
from contextlib import nullcontext, suppress
from locale import strxfrm
from os.path import normcase
from pathlib import PurePath
from typing import Any, Iterator, Mapping, Sequence

from pynvim_pp.buffer import Buffer
from pynvim_pp.hold import hold_win
from pynvim_pp.lib import display_width
from pynvim_pp.logging import log
from pynvim_pp.nvim import Nvim
from pynvim_pp.operators import operator_marks
from pynvim_pp.preview import set_preview
from pynvim_pp.rpc_types import NvimError
from pynvim_pp.types import NoneType
from pynvim_pp.window import Window
from std2.argparse import ArgparseError, ArgParser
from std2.locale import pathsort_key
from yaml import SafeDumper, add_representer, safe_dump_all
from yaml.nodes import ScalarNode, SequenceNode

from ...clients.snippet.worker import Worker as SnipWorker
from ...consts import REPL_GRAMMAR
from ...lang import LANG
from ...paths.show import fmt_path
from ...registry import rpc
from ...shared.types import SnippetGrammar
from ...snippets.parsers.lexer import decode_mark_idx
from ...snippets.parsers.types import ParseError, ParseInfo
from ...snippets.types import LoadError
from ..rt_types import Stack
from .snippets import (
    Compiled,
    compile_one,
    compile_user_snippets,
    slurp_compiled,
    snippet_paths,
    user_mtimes,
)

_WIDTH = 80
_TAB = 2


def _repr_str(dumper: SafeDumper, data: str) -> ScalarNode:
    if len(data.splitlines()) > 1:
        style = "|"
    elif display_width(data, tabsize=_TAB) > _WIDTH:
        style = ">"
    else:
        style = ""
    node = dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)
    return node


def _repr_seq(dumper: SafeDumper, data: Sequence[Any]) -> SequenceNode:
    node = dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True)
    return node


add_representer(str, _repr_str, Dumper=SafeDumper)
add_representer(list, _repr_seq, Dumper=SafeDumper)


def _fmt_yaml(data: Sequence[Any]) -> str:
    yaml = safe_dump_all(
        data,
        allow_unicode=True,
        explicit_start=True,
        sort_keys=False,
        width=_WIDTH,
        indent=_TAB,
    )
    return str(yaml)


def _pprn(
    compiled: Compiled,
) -> str:
    def cont() -> Iterator[Mapping[str, Any]]:
        sorted_exts = sorted(compiled.exts, key=strxfrm)
        if sorted_exts:
            mapping: Mapping[str, Any] = {"extends": sorted_exts}
            yield mapping

        for parsed, edit, marks, text_trans in compiled.parsed:
            mapping = {}
            if parsed.label:
                mapping.update(label=parsed.label)

            mapping.update(
                matches=sorted(parsed.matches, key=strxfrm),
                expanded=edit.new_text.expandtabs(_TAB),
            )

            if sorted_marks := [
                [decode_mark_idx(m.idx), m.text]
                for m in sorted(marks, key=lambda m: (m.begin, m.end))
            ]:
                mapping.update(marks=sorted_marks)

            if trans := tuple(f"${{{idx}}}" for idx, _ in sorted(text_trans.items())):
                mapping.update(filters=trans)

            yield mapping

    return _fmt_yaml(tuple(cont()))


@rpc()
async def eval_snips(
    stack: Stack,
    visual: bool,
    maybe_grammar: str = REPL_GRAMMAR,
) -> None:
    try:
        grammar = SnippetGrammar[maybe_grammar]
    except KeyError:
        grammar = SnippetGrammar.lsp
        log.warning("%s", "bad snippet grammar -- reverting to LSP")

    win = await Window.get_current()
    buf = await win.get_buf()
    line_count = await buf.line_count()
    path = PurePath(normcase(await buf.get_name() or ""))
    comment_str = await buf.commentstr() or ("", "")
    clipboard = await Nvim.fn.getreg(str)
    info = ParseInfo(visual="", clipboard=clipboard, comment_str=comment_str)

    if visual:
        (lo, _), (hi, _) = await operator_marks(buf=buf, visual_type=None)
        hi = min(line_count, hi + 1)
    else:
        lo, hi = 0, line_count

    lines = await buf.get_lines(lo=lo, hi=hi)

    try:
        compiled = compile_one(
            stack,
            grammar=grammar,
            path=path,
            info=info,
            lines=enumerate(lines, start=lo + 1),
        )
    except (LoadError, ParseError) as e:
        preview = str(e).splitlines()
        async with hold_win(win=win):
            await set_preview(syntax="", preview=preview)
        await Nvim.write(LANG("snip parse fail"))
    else:
        preview = _pprn(compiled).splitlines()
        async with hold_win(win=win):
            await set_preview(syntax="yaml", preview=preview)
        if preview:
            await Nvim.write(LANG("snip parse succ"))
        else:
            await Nvim.write(LANG("no snippets found"))

        with suppress(NvimError, LoadError, ParseError):
            await Nvim.exec(":silent! write")
            await compile_user_snippets(stack)
            await slurp_compiled(stack, warn=frozenset(), silent=True)


def _parse_args(args: Sequence[str], filetype: str) -> Namespace:
    parser = ArgParser()
    sub_parsers = parser.add_subparsers(dest="action", required=True)

    sub_parsers.add_parser("ls")
    sub_parsers.add_parser("cd")
    sub_parsers.add_parser("compile")

    with nullcontext(sub_parsers.add_parser("edit")) as p:
        p.add_argument("filetype", nargs="?", default=filetype)

    return parser.parse_args(args)


@rpc()
async def snips(stack: Stack, args: Sequence[str]) -> None:
    buf = await Buffer.get_current()
    ft = await buf.filetype()

    try:
        ns = _parse_args(args, filetype=ft or "*")
    except ArgparseError as e:
        await Nvim.write(e, error=True)

    else:
        if ns.action == "ls":
            cwd = await Nvim.getcwd()
            _, mtimes = await user_mtimes(
                user_path=stack.settings.clients.snippets.user_path
            )
            preview: Sequence[str] = tuple(
                fmt_path(cwd, path=path, is_dir=False)
                for path in sorted(mtimes, key=pathsort_key)
            )

            if mtimes:
                await set_preview(syntax="", preview=preview)
            else:
                await Nvim.write(LANG("no snippets found"))

        elif ns.action == "cd":
            paths = await snippet_paths(
                user_path=stack.settings.clients.snippets.user_path
            )
            if paths:
                path, *_ = paths
                path.mkdir(parents=True, exist_ok=True)
                await Nvim.chdir(path, history=True)
            else:
                assert False

        elif ns.action == "compile":
            for worker in stack.workers:
                if isinstance(worker, SnipWorker):
                    await Nvim.write(LANG("waiting..."))
                    try:
                        await compile_user_snippets(stack)
                        await slurp_compiled(stack, warn=frozenset(), silent=False)
                    except (LoadError, ParseError) as e:
                        preview = str(e).splitlines()
                        await set_preview(syntax="", preview=preview)
                        await Nvim.write(LANG("snip parse fail"))

                    else:
                        await Nvim.write(LANG("snip parse succ"))
                    break
            else:
                await Nvim.write(LANG("snip source not enabled"))

        elif ns.action == "edit":
            paths, mtimes = await user_mtimes(
                user_path=stack.settings.clients.snippets.user_path
            )
            path, *_ = paths
            exts = {path.stem: path for path in mtimes}
            snip_path = exts.get(ns.filetype, path / f"{ns.filetype}.snip")
            snip_path.parent.mkdir(parents=True, exist_ok=True)

            escaped = await Nvim.fn.fnameescape(str, normcase(snip_path))
            await Nvim.api.feedkeys(NoneType, f":edit {escaped}", "n", False)

        else:
            assert False
