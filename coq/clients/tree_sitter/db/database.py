from contextlib import closing, suppress
from sqlite3 import Connection, Cursor, OperationalError
from typing import Iterable, Iterator, Mapping

from ....consts import TREESITTER_DB
from ....databases.types import DB
from ....shared.settings import MatchOptions
from ....shared.sql import init_db, like_esc
from ....treesitter.types import Payload, SimplePayload
from .sql import sql


def _init() -> Connection:
    conn = Connection(TREESITTER_DB, isolation_level=None)
    init_db(conn)
    conn.executescript(sql("create", "pragma"))
    conn.executescript(sql("create", "tables"))
    return conn


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


class TDB(DB):
    def __init__(self) -> None:
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
                    sql("delete", "words"),
                    (
                        {"buffer_id": buf_id, "lo": line_count, "hi": -1}
                        for buf_id, line_count in live_bufs.items()
                    ),
                )
                cursor.execute("PRAGMA optimize", ())

    def populate(
        self,
        buf_id: int,
        filetype: str,
        filename: str,
        lo: int,
        hi: int,
        nodes: Iterable[Payload],
    ) -> None:
        def m1() -> Iterator[Mapping]:
            for node in nodes:
                lo, hi = node.range if node.range else (None, None)
                yield {
                    "buffer_id": buf_id,
                    "lo": lo,
                    "hi": hi,
                    "word": node.text,
                    "kind": node.kind,
                    "pword": node.parent.text if node.parent else None,
                    "pkind": node.parent.kind if node.parent else None,
                    "gpword": node.grandparent.text if node.grandparent else None,
                    "gpkind": node.grandparent.kind if node.grandparent else None,
                }

        with suppress(OperationalError):
            with self._conn, closing(self._conn.cursor()) as cursor:
                _ensure_buffer(
                    cursor, buf_id=buf_id, filetype=filetype, filename=filename
                )
                cursor.execute(
                    sql("delete", "words"),
                    {"buffer_id": buf_id, "lo": lo, "hi": hi},
                )
                with suppress(UnicodeEncodeError):
                    cursor.executemany(sql("insert", "word"), m1())

    def select(
        self,
        opts: MatchOptions,
        filetype: str,
        word: str,
        sym: str,
        limit: int,
    ) -> Iterator[Payload]:
        with suppress(OperationalError):
            with self._conn, closing(self._conn.cursor()) as cursor:
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
                    range = row["lo"], row["hi"]
                    grandparent = (
                        SimplePayload(text=row["gpword"], kind=row["gpkind"])
                        if row["gpword"] and row["gpkind"]
                        else None
                    )
                    parent = (
                        SimplePayload(text=row["pword"], kind=row["pkind"])
                        if row["pword"] and row["pkind"]
                        else None
                    )
                    yield Payload(
                        filename=row["filename"],
                        range=range,
                        text=row["word"],
                        kind=row["kind"],
                        parent=parent,
                        grandparent=grandparent,
                    )
