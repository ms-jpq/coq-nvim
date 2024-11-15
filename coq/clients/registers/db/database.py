from contextlib import closing, suppress
from dataclasses import dataclass
from sqlite3 import Connection, Cursor, OperationalError
from typing import AbstractSet, Any, Iterator, Mapping

from ....consts import REGISTER_DB
from ....databases.types import DB
from ....shared.parse import coalesce, tokenize
from ....shared.settings import MatchOptions
from ....shared.sql import init_db, like_esc
from .sql import sql


@dataclass(frozen=True)
class RegWord:
    linewise: bool
    match: str
    regname: str
    text: str


def _init() -> Connection:
    conn = Connection(REGISTER_DB, isolation_level=None)
    init_db(conn)
    conn.executescript(sql("create", "pragma"))
    conn.executescript(sql("create", "tables"))
    return conn


class RDB(DB):
    def __init__(
        self,
        tokenization_limit: int,
        unifying_chars: AbstractSet[str],
        include_syms: bool,
    ) -> None:
        self._tokenization_limit = tokenization_limit
        self._unifying_chars = unifying_chars
        self._include_syms = include_syms
        self._conn = _init()

    def periodical(
        self,
        wordreg: Mapping[str, str],
        linereg: Mapping[str, str],
    ) -> None:
        m1 = (*wordreg, *linereg)

        def m2() -> Iterator[Mapping]:
            for reg, text in wordreg.items():
                for word in tokenize(
                    self._tokenization_limit,
                    unifying_chars=self._unifying_chars,
                    include_syms=self._include_syms,
                    text=text,
                ):
                    yield {"register": reg, "word": word}

        def m3() -> Iterator[Mapping]:
            for reg, text in linereg.items():
                for row in text.splitlines():
                    if line := row.strip():
                        tokens = coalesce(
                            self._unifying_chars,
                            include_syms=True,
                            backwards=False,
                            chars=line,
                        )
                        if word := next(tokens, None):
                            yield {"register": reg, "word": word, "line": line}

        with suppress(OperationalError):
            with self._conn, closing(self._conn.cursor()) as cursor:
                cursor.executemany(sql("delete", "register"), m1)
                cursor.executemany(sql("insert", "register"), m1)
                with suppress(UnicodeEncodeError):
                    cursor.executemany(sql("insert", "word"), m2())
                with suppress(UnicodeEncodeError):
                    cursor.executemany(sql("insert", "line"), m3())
                cursor.execute("PRAGMA optimize", ())

    def select(
        self,
        linewise: bool,
        match_syms: bool,
        opts: MatchOptions,
        word: str,
        sym: str,
        limit: int,
    ) -> Iterator[RegWord]:
        def fetch(
            cursor: Cursor, match_syms: bool, stmt: str, linewise: bool
        ) -> Iterator[Any]:
            cursor.execute(
                sql("select", stmt),
                {
                    "cut_off": opts.fuzzy_cutoff,
                    "look_ahead": opts.look_ahead,
                    "limit": limit,
                    "word": word,
                    "sym": (sym if match_syms else ""),
                    "like_word": like_esc(word[: opts.exact_matches]),
                    "like_sym": like_esc(sym[: opts.exact_matches]),
                },
            )
            for row in cursor:
                yield RegWord(
                    linewise=linewise,
                    match=row["word"],
                    regname=row["register"],
                    text=row["text"],
                )

        with suppress(OperationalError):
            with self._conn, closing(self._conn.cursor()) as cursor:
                yield from (
                    fetch(cursor, match_syms=True, stmt="lines", linewise=True)
                    if linewise
                    else ()
                )
                yield from fetch(
                    cursor, match_syms=match_syms, stmt="words", linewise=False
                )
