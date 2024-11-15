from contextlib import closing, suppress
from dataclasses import dataclass
from itertools import islice
from random import shuffle
from sqlite3 import Connection, OperationalError
from sqlite3.dbapi2 import Cursor
from typing import AbstractSet, Iterator, Mapping, Optional, Sequence, Tuple
from uuid import uuid4

from pynvim_pp.lib import recode

from ....consts import BUFFER_DB, DEBUG
from ....databases.types import DB
from ....shared.parse import coalesce
from ....shared.settings import MatchOptions
from ....shared.sql import BIGGEST_INT, init_db, like_esc
from .sql import sql


@dataclass(frozen=True)
class Update:
    buf_id: int
    filetype: str
    filename: str
    lo: int
    hi: int
    lines: Sequence[str]


@dataclass(frozen=True)
class BufferWord:
    text: str
    filetype: str
    filename: str
    line_num: int


def _ensure_buffer(cursor: Cursor, buf_id: int, filetype: str, filename: str) -> None:
    cursor.execute(sql("select", "buffer_by_id"), {"rowid": buf_id})
    row = {
        "rowid": buf_id,
        "filetype": filetype,
        "filename": filename,
    }
    if cursor.fetchone():
        cursor.execute(sql("update", "buffer"), row)
    else:
        cursor.execute(sql("insert", "buffer"), row)


def _setlines(
    cursor: Cursor,
    unifying_chars: AbstractSet[str],
    tokenization_limit: int,
    include_syms: bool,
    buf_id: int,
    filetype: str,
    filename: str,
    lo: int,
    hi: int,
    lines: Sequence[str],
) -> None:
    def m0() -> Iterator[Tuple[int, str, bytes]]:
        for line_num, line in enumerate(lines, start=lo):
            line_id = uuid4().bytes
            yield line_num, recode(line), line_id

    line_info = [*m0()]
    shuffle(line_info)

    def m1() -> Iterator[Mapping]:
        for line_num, line, line_id in line_info:
            yield {
                "rowid": line_id,
                "buffer_id": buf_id,
                "line_num": line_num,
                "line": line if DEBUG else "",
            }

    def m2() -> Iterator[Mapping]:
        for line_num, line, line_id in line_info:
            for word in coalesce(
                unifying_chars,
                include_syms=include_syms,
                backwards=None,
                chars=line,
            ):
                yield {"line_id": line_id, "word": word, "line_num": line_num}

    _ensure_buffer(
        cursor,
        buf_id=buf_id,
        filetype=filetype,
        filename=filename,
    )
    cursor.execute(
        sql("delete", "lines"),
        {"buffer_id": buf_id, "lo": lo, "hi": hi},
    )
    shift = len(lines) - (hi - lo)
    cursor.execute(
        sql("update", "lines_shift_1"),
        {"buffer_id": buf_id, "lo": lo, "shift": shift},
    )
    cursor.execute(sql("update", "lines_shift_2"), {"buffer_id": buf_id})
    with suppress(UnicodeEncodeError):
        cursor.executemany(sql("insert", "line"), m1())
    with suppress(UnicodeEncodeError):
        cursor.executemany(sql("insert", "word"), islice(m2(), tokenization_limit))
    cursor.execute(sql("select", "line_count"), {"buffer_id": buf_id})
    count = cursor.fetchone()["line_count"]
    if not count:
        cursor.execute(
            sql("insert", "line"),
            {"rowid": uuid4().bytes, "line": "", "buffer_id": buf_id, "line_num": 0},
        )


def _init() -> Connection:
    conn = Connection(BUFFER_DB, isolation_level=None)
    init_db(conn)
    conn.executescript(sql("create", "pragma"))
    conn.executescript(sql("create", "tables"))
    return conn


class BDB(DB):
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

    def vacuum(self, live_bufs: Mapping[int, int]) -> None:
        with suppress(OperationalError):
            with self._conn, closing(self._conn.cursor()) as cursor:
                cursor.execute(sql("select", "buffers"), ())
                existing = {row["rowid"] for row in cursor.fetchall()}
                dead = existing - live_bufs.keys()
                cursor.executemany(
                    sql("delete", "buffer"),
                    ({"buffer_id": buf_id} for buf_id in dead),
                )
                cursor.executemany(
                    sql("delete", "lines"),
                    (
                        {"buffer_id": buf_id, "lo": line_count, "hi": -1}
                        for buf_id, line_count in live_bufs.items()
                    ),
                )
                cursor.execute("PRAGMA optimize", ())

    def buf_update(self, buf_id: int, filetype: str, filename: str) -> None:
        with self._conn, closing(self._conn.cursor()) as cursor:
            _ensure_buffer(
                cursor,
                buf_id=buf_id,
                filetype=filetype,
                filename=filename,
            )

    def set_lines(
        self,
        buf_id: int,
        filetype: str,
        filename: str,
        lo: int,
        hi: int,
        lines: Sequence[str],
    ) -> None:
        with suppress(OperationalError):
            with self._conn, closing(self._conn.cursor()) as cursor:
                _setlines(
                    cursor,
                    unifying_chars=self._unifying_chars,
                    tokenization_limit=self._tokenization_limit,
                    include_syms=self._include_syms,
                    buf_id=buf_id,
                    filetype=filetype,
                    filename=filename,
                    lo=lo,
                    hi=hi,
                    lines=lines,
                )

    def words(
        self,
        opts: MatchOptions,
        filetype: Optional[str],
        word: str,
        sym: str,
        limit: int,
        update: Optional[Update],
    ) -> Iterator[BufferWord]:
        with suppress(OperationalError):
            with self._conn, closing(self._conn.cursor()) as cursor:
                if update:
                    _setlines(
                        cursor,
                        unifying_chars=self._unifying_chars,
                        tokenization_limit=self._tokenization_limit,
                        include_syms=self._include_syms,
                        buf_id=update.buf_id,
                        filetype=update.filetype,
                        filename=update.filename,
                        lo=update.lo,
                        hi=update.hi,
                        lines=update.lines,
                    )

                cursor.execute(
                    sql("select", "words"),
                    {
                        "cut_off": opts.fuzzy_cutoff,
                        "look_ahead": opts.look_ahead,
                        "limit": limit,
                        "filetype": filetype,
                        "word": word,
                        "sym": sym,
                        "like_word": like_esc(word[: opts.exact_matches]),
                        "like_sym": like_esc(sym[: opts.exact_matches]),
                    },
                )
                for row in cursor:
                    yield BufferWord(
                        text=row["word"],
                        filetype=row["filetype"],
                        filename=row["filename"],
                        line_num=row["line_num"] + 1,
                    )
