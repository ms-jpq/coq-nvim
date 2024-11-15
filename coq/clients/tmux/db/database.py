from contextlib import closing, suppress
from dataclasses import dataclass
from sqlite3 import Connection, OperationalError
from typing import AbstractSet, Iterator, Mapping, MutableMapping, Optional

from ....consts import TMUX_DB
from ....databases.types import DB
from ....shared.parse import tokenize
from ....shared.settings import MatchOptions
from ....shared.sql import init_db, like_esc
from ....tmux.parse import Pane
from .sql import sql


@dataclass(frozen=True)
class TmuxWord:
    text: str
    session_name: str
    window_index: int
    window_name: str
    pane_index: int
    pane_title: str


def _init() -> Connection:
    conn = Connection(TMUX_DB, isolation_level=None)
    init_db(conn)
    conn.executescript(sql("create", "pragma"))
    conn.executescript(sql("create", "tables"))
    return conn


class TMDB(DB):
    def __init__(
        self,
        tokenization_limit: int,
        unifying_chars: AbstractSet[str],
        include_syms: bool,
    ) -> None:
        self._current: Optional[Pane] = None
        self._tokenization_limit = tokenization_limit
        self._unifying_chars = unifying_chars
        self._include_syms = include_syms
        self._cache: MutableMapping[str, str] = {}
        self._conn = _init()

    def periodical(self, current: Optional[Pane], panes: Mapping[Pane, str]) -> None:
        self._current = current
        live_panes = {pane.uid for pane in panes}
        self._cache = {
            pane_id: text
            for pane_id, text in self._cache.items()
            if pane_id in live_panes
        }
        not_cached = {
            pane: text
            for pane, text in panes.items()
            if pane != current and text != self._cache.get(pane.uid)
        }

        def m1(existing: AbstractSet[str]) -> Iterator[Mapping]:
            for uid in existing - live_panes:
                yield {"pane_id": uid}

        def m2() -> Iterator[Mapping]:
            for pane in not_cached:
                yield {
                    "pane_id": pane.uid,
                    "session_name": pane.session_name,
                    "window_index": pane.window_index,
                    "window_name": pane.window_name,
                    "pane_index": pane.pane_index,
                    "pane_title": pane.pane_title,
                }

        def m3() -> Iterator[Mapping]:
            for pane, text in not_cached.items():
                for word in tokenize(
                    self._tokenization_limit,
                    unifying_chars=self._unifying_chars,
                    include_syms=self._include_syms,
                    text=text,
                ):
                    yield {
                        "pane_id": pane.uid,
                        "word": word,
                    }
                else:
                    self._cache[pane.uid] = text

        with suppress(OperationalError):
            with self._conn, closing(self._conn.cursor()) as cursor:
                cursor.execute(sql("select", "panes"))
                existing = {row["pane_id"] for row in cursor.fetchall()}
                cursor.executemany(sql("delete", "pane"), m1(existing))
                cursor.executemany(sql("insert", "pane"), m2())
                with suppress(UnicodeEncodeError):
                    cursor.executemany(sql("insert", "word"), m3())
                cursor.execute("PRAGMA optimize", ())

    def select(
        self, opts: MatchOptions, word: str, sym: str, limit: int
    ) -> Iterator[TmuxWord]:
        with suppress(OperationalError):
            with self._conn, closing(self._conn.cursor()) as cursor:
                cursor.execute(
                    sql("select", "words"),
                    {
                        "cut_off": opts.fuzzy_cutoff,
                        "look_ahead": opts.look_ahead,
                        "limit": limit,
                        "pane_id": self._current.uid if self._current else None,
                        "word": word,
                        "sym": sym,
                        "like_word": like_esc(word[: opts.exact_matches]),
                        "like_sym": like_esc(sym[: opts.exact_matches]),
                    },
                )
                for row in cursor:
                    yield TmuxWord(
                        text=row["word"],
                        session_name=row["session_name"],
                        window_index=row["window_index"],
                        window_name=row["window_name"],
                        pane_index=row["pane_index"],
                        pane_title=row["pane_title"],
                    )
