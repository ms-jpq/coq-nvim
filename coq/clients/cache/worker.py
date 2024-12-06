from dataclasses import dataclass, replace
from itertools import chain
from typing import (
    AbstractSet,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    MutableSet,
    Optional,
    Tuple,
)
from uuid import UUID, uuid4

from ...shared.fuzzy import multi_set_ratio
from ...shared.parse import coalesce
from ...shared.repeat import sanitize
from ...shared.runtime import Supervisor
from ...shared.settings import MatchOptions
from ...shared.timeit import timeit
from ...shared.types import (
    BaseRangeEdit,
    Completion,
    Context,
    Cursors,
    Interruptible,
    SnippetEdit,
)
from .db.database import Database


@dataclass(frozen=True)
class _CacheCtx:
    change_id: UUID
    commit_id: UUID
    buf_id: int
    row: int
    col: int
    syms_before: str


def _use_cache(match: MatchOptions, cache: _CacheCtx, ctx: Context) -> bool:
    row, _ = ctx.position
    use_cache = (
        not ctx.manual
        and cache.commit_id == ctx.commit_id
        and ctx.buf_id == cache.buf_id
        and row == cache.row
        and multi_set_ratio(
            ctx.syms_before, cache.syms_before, look_ahead=match.look_ahead
        )
        >= match.fuzzy_cutoff
    )
    return use_cache


def _overlap(row: int, edit: BaseRangeEdit) -> bool:
    (b_row, _), (e_row, _) = edit.begin, edit.end
    return b_row == row or e_row == row


def sanitize_cached(
    cursor: Cursors, comp: Completion, sort_by: Optional[str]
) -> Optional[Completion]:
    if edit := sanitize(cursor, edit=comp.primary_edit):
        row, *_ = cursor
        cached = replace(
            comp,
            primary_edit=edit,
            secondary_edits=tuple(
                edit for edit in comp.secondary_edits if not _overlap(row, edit=edit)
            ),
            sort_by=sort_by or comp.sort_by,
        )
        return cached
    else:
        return None


class CacheWorker(Interruptible):
    def __init__(self, supervisor: Supervisor) -> None:
        self._supervisor = supervisor
        self._db = Database()
        self._cache_ctx = _CacheCtx(
            change_id=uuid4(),
            commit_id=uuid4(),
            buf_id=-1,
            row=-1,
            col=-1,
            syms_before="",
        )
        self._clients: MutableSet[str] = set()
        self._cached: MutableMapping[bytes, Completion] = {}

    def interrupt(self) -> None:
        self._db.interrupt()

    def set_cache(
        self,
        items: Mapping[Optional[str], Iterable[Completion]],
        skip_db: bool,
    ) -> None:
        new_comps = {
            comp.uid.bytes: comp for comp in chain.from_iterable(items.values())
        }

        def cont() -> Iterator[Tuple[bytes, str]]:
            for key, val in new_comps.items():
                if self._supervisor.comp.smart:
                    for word in coalesce(
                        self._supervisor.match.unifying_chars,
                        include_syms=True,
                        backwards=None,
                        chars=val.sort_by,
                    ):
                        yield key, word
                else:
                    yield key, val.sort_by

        if not skip_db:
            self._db.insert(cont())

        for client in items:
            if client:
                self._clients.add(client)
        self._cached.update(new_comps)

    def apply_cache(
        self, context: Context, always: bool
    ) -> Tuple[bool, AbstractSet[str], Iterator[Completion]]:
        cache_ctx = self._cache_ctx
        row, col = context.position
        self._cache_ctx = _CacheCtx(
            change_id=context.change_id,
            commit_id=context.commit_id,
            buf_id=context.buf_id,
            row=row,
            col=col,
            syms_before=context.syms_before,
        )

        use_cache = _use_cache(
            self._supervisor.match, cache=cache_ctx, ctx=context
        ) and bool(self._cached)
        cached_clients = {*self._clients}

        if not use_cache:
            self._clients.clear()
            self._cached.clear()

        selected = (
            ((key, val.sort_by) for key, val in self._cached.items())
            if always
            else self._db.select(
                not use_cache,
                opts=self._supervisor.match,
                word=context.words,
                sym=context.syms,
                limitless=context.manual,
            )
        )

        def get() -> Iterator[Completion]:
            with timeit("CACHE -- GET"):
                for key, sort_by in selected:
                    if (comp := self._cached.get(key)) and (
                        cached := sanitize_cached(
                            context.cursor, comp=comp, sort_by=sort_by
                        )
                    ):
                        if (
                            context.words.startswith(sort_by)
                            or context.syms.startswith(sort_by)
                        ) and not (
                            isinstance(cached.primary_edit, SnippetEdit)
                            or cached.secondary_edits
                            or cached.extern
                            or cached.always_on_top
                        ):
                            continue
                        yield cached

        return use_cache, cached_clients, get()
