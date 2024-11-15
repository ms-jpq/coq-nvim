from asyncio import as_completed
from contextlib import suppress
from itertools import chain, islice
from os import environ, scandir
from os.path import altsep, curdir, expanduser, expandvars, normpath, pardir, sep, split
from pathlib import Path
from string import ascii_letters, digits
from typing import (
    AbstractSet,
    AsyncIterator,
    Iterator,
    MutableSequence,
    MutableSet,
    Tuple,
)

from std2.asyncio import to_thread
from std2.platform import OS, os
from std2.string import removesuffix

from ...shared.context import cword_before
from ...shared.executor import AsyncExecutor
from ...shared.fuzzy import quick_ratio
from ...shared.parse import lower
from ...shared.runtime import Supervisor
from ...shared.runtime import Worker as BaseWorker
from ...shared.settings import PathResolution, PathsClient
from ...shared.sql import BIGGEST_INT
from ...shared.types import Completion, Context, Edit, ExternPath

_DRIVE_LETTERS = {*ascii_letters}
_SH_VAR_CHARS = {*ascii_letters, *digits, "_"}
_SEP_CHARS = sep + altsep if altsep else sep

# if os is OS.windows:
#     _USERS = ()
# else:
#     from pwd import getpwall
#     _USERS = {p.pw_name for p in getpwall()}


def p_lhs(os: OS, lhs: str) -> str:
    # TODO -- expand ~user
    for sym in (pardir, curdir, "~"):
        if lhs.endswith(sym):
            return sym
    else:
        if os is OS.windows and lhs.endswith(":"):
            maybe_drive = removesuffix(lhs, suffix=":")[-1:]
            return maybe_drive + ":" if maybe_drive in _DRIVE_LETTERS else ""
        elif os is OS.windows and lhs.endswith("%"):
            _, s, r = removesuffix(lhs, suffix="%").rpartition("%")
            return s + r + s if s and {*r}.issubset(_SH_VAR_CHARS) else ""
        elif lhs.endswith("}"):
            _, s, r = lhs.rpartition("${")
            return (
                s + r
                if s and {*removesuffix(r, suffix="}")}.issubset(_SH_VAR_CHARS)
                else ""
            )
        else:
            _, s, r = lhs.rpartition("$")
            return s + r if s and {*r}.issubset(_SH_VAR_CHARS) and r in environ else ""


def _split(sep: str, text: str) -> Iterator[str]:
    acc: MutableSequence[str] = []
    for char in text:
        if char == sep:
            yield "".join(acc)
            acc.clear()
        acc.append(char)
    if acc:
        yield "".join(acc)


def separate(seps: AbstractSet[str], line: str) -> Iterator[str]:
    if not seps:
        yield line
    else:
        sep = next(iter(seps))
        for l in _split(sep, line):
            yield from separate(seps - {sep}, l)


def segs(seps: AbstractSet[str], line: str) -> Iterator[str]:
    segments = tuple(separate(seps, line=line))
    for idx in range(1, len(segments)):
        lhs, rhs = segments[idx - 1 : idx], segments[idx:]
        l = p_lhs(os, lhs="".join(lhs))
        yield "".join(chain((l,), rhs))


def _iter_segs(seps: AbstractSet[str], line: str) -> Iterator[Tuple[str, str]]:
    for segment in segs(seps, line=line):
        s1 = segment
        s2 = expanduser(s1) if s1.startswith("~") else s1
        s3 = expandvars(s2)

        for idx, s0 in enumerate((s1, s2, s3)):
            if idx and s0 == s1:
                pass
            else:
                yield segment, s0


def _p_sep(line_pre: str) -> str:
    i1, i2 = line_pre.rfind(sep), line_pre.rfind(altsep or sep)
    return (altsep or sep) if i2 > i1 else sep


def _join(local_sep: str, lhs: str, rhs: str) -> str:
    l, r = split(lhs)
    return (
        l.rstrip(_SEP_CHARS)
        + local_sep
        + normpath(r.rstrip(_SEP_CHARS) + local_sep + rhs.lstrip(_SEP_CHARS)).lstrip(
            _SEP_CHARS
        )
    )


def parse(
    seps: AbstractSet[str],
    look_ahead: int,
    fuzzy_cutoff: float,
    base: Path,
    line: str,
) -> Iterator[Tuple[Path, bool, str]]:
    for segment, s0 in _iter_segs(seps, line=line):
        local_sep = _p_sep(s0)
        if not {*s0}.issubset({sep, altsep}):
            p = Path(s0)
            entire = p if p.is_absolute() else base / p

            with suppress(OSError):
                if entire.is_dir():
                    for path in scandir(entire):
                        is_dir = path.is_dir()
                        term = local_sep if is_dir else ""
                        line = _join(local_sep, lhs=segment, rhs=path.name) + term
                        yield Path(path.path), is_dir, line
                    return

                else:
                    lft, go, rhs = s0.rpartition(local_sep)
                    if go:
                        lp, sp, _ = segment.rpartition(local_sep)
                        lseg = lp + sp

                        lhs = lft + go
                        p = Path(lhs)
                        left = p if p.is_absolute() else base / p
                        if left.is_dir():
                            for path in scandir(left):
                                ratio = quick_ratio(
                                    lower(rhs),
                                    lower(path.name),
                                    look_ahead=look_ahead,
                                )
                                if (
                                    ratio >= fuzzy_cutoff
                                    and len(path.name) + look_ahead >= len(rhs)
                                    and not rhs.startswith(path.name)
                                ):
                                    is_dir = path.is_dir()
                                    term = local_sep if is_dir else ""
                                    line = (
                                        _join(local_sep, lhs=lseg, rhs=path.name) + term
                                    )
                                    yield Path(path.path), is_dir, line
                            return


async def _parse(
    base: Path,
    line: str,
    seps: AbstractSet[str],
    limit: int,
    look_ahead: int,
    fuzzy_cutoff: float,
) -> AbstractSet[Tuple[Path, bool, str]]:
    def cont() -> AbstractSet[Tuple[Path, bool, str]]:
        return {
            *islice(
                parse(
                    seps,
                    look_ahead=look_ahead,
                    fuzzy_cutoff=fuzzy_cutoff,
                    base=base,
                    line=line,
                ),
                limit,
            )
        }

    return await to_thread(cont)


def _sort_by(unifying_chars: AbstractSet[str], context: Context, new_text: str) -> str:
    chars = [*new_text]
    if new_text.endswith(sep) or (altsep and new_text.endswith(altsep)):
        end = chars.pop()
    else:
        end = ""

    tmp = "".join(chars)
    cword = cword_before(unifying_chars, lower=False, context=context, sort_by=tmp)
    sort_by = f"{cword}{end}"
    return sort_by


class Worker(BaseWorker[PathsClient, None]):
    def __init__(
        self,
        ex: AsyncExecutor,
        supervisor: Supervisor,
        always_wait: bool,
        options: PathsClient,
        misc: None,
    ) -> None:
        super().__init__(
            ex,
            supervisor=supervisor,
            always_wait=always_wait,
            options=options,
            misc=misc,
        )
        seps = {sep, altsep} if altsep else {sep}
        self._seps = {sep for sep in options.path_seps if sep in seps} or seps

    def interrupt(self) -> None:
        pass

    async def _work(self, context: Context) -> AsyncIterator[Completion]:
        async with self._work_lock:
            line = context.line_before + context.words_after

            def cont() -> Iterator[Path]:
                if PathResolution.cwd in self._options.resolution:
                    yield Path(context.cwd)

                if PathResolution.file in self._options.resolution:
                    yield Path(context.filename).parent

            base_paths = {*cont()}

            limit = (
                BIGGEST_INT
                if context.manual
                else self._options.max_pulls or self._supervisor.match.max_results
            )
            aw = tuple(
                _parse(
                    p,
                    line=line,
                    seps=self._seps,
                    limit=limit,
                    look_ahead=self._supervisor.match.look_ahead,
                    fuzzy_cutoff=self._supervisor.match.fuzzy_cutoff,
                )
                for p in base_paths
            )
            seen: MutableSet[str] = set()

            for co in as_completed(aw):
                for path, is_dir, new_text in await co:
                    if len(seen) >= limit:
                        break
                    elif new_text not in seen:
                        seen.add(new_text)
                        edit = Edit(new_text=new_text)
                        completion = Completion(
                            source=self._options.short_name,
                            always_on_top=self._options.always_on_top,
                            weight_adjust=self._options.weight_adjust,
                            label=edit.new_text,
                            sort_by=_sort_by(
                                self._supervisor.match.unifying_chars,
                                context=context,
                                new_text=new_text,
                            ),
                            primary_edit=edit,
                            adjust_indent=False,
                            extern=ExternPath(is_dir=is_dir, path=path),
                            icon_match="Folder" if new_text.endswith(sep) else "File",
                        )
                        yield completion
