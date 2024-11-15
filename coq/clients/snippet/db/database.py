from contextlib import closing, suppress
from os.path import normcase
from pathlib import Path, PurePath
from sqlite3 import Connection, OperationalError
from typing import AbstractSet, Iterator, Mapping, TypedDict, cast
from uuid import uuid4

from ....databases.types import DB
from ....shared.settings import MatchOptions
from ....shared.sql import BIGGEST_INT, init_db, like_esc
from ....snippets.types import LoadedSnips
from .sql import sql

_SCHEMA = "v4"


class _Snip(TypedDict):
    grammar: str
    word: str
    snippet: str
    label: str
    doc: str


def _init(db_dir: Path) -> Connection:
    db = (db_dir / _SCHEMA).with_suffix(".sqlite3")
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = Connection(db, isolation_level=None)
    init_db(conn)
    conn.executescript(sql("create", "pragma"))
    conn.executescript(sql("create", "tables"))
    return conn


class SDB(DB):
    def __init__(self, vars_dir: Path) -> None:
        db_dir = vars_dir / "clients" / "snippets"
        self._conn = _init(db_dir)

    def clean(self, paths: AbstractSet[PurePath]) -> None:
        with self._conn, closing(self._conn.cursor()) as cursor:
            cursor.executemany(
                sql("delete", "source"),
                ({"filename": normcase(path)} for path in paths),
            )

    def mtimes(self) -> Mapping[PurePath, float]:
        with self._conn, closing(self._conn.cursor()) as cursor:
            cursor.execute(sql("select", "sources"), ())
            return {
                PurePath(row["filename"]): row["mtime"] for row in cursor.fetchall()
            }

    def populate(self, path: PurePath, mtime: float, loaded: LoadedSnips) -> None:
        with self._conn, closing(self._conn.cursor()) as cursor:
            filename, source_id = normcase(path), uuid4().bytes
            cursor.execute(sql("delete", "source"), {"filename": filename})
            cursor.execute(
                sql("insert", "source"),
                {"rowid": source_id, "filename": filename, "mtime": mtime},
            )

            for src, dests in loaded.exts.items():
                for dest in dests:
                    cursor.executemany(
                        sql("insert", "filetype"),
                        ({"filetype": src}, {"filetype": dest}),
                    )
                    cursor.execute(
                        sql("insert", "extension"),
                        {"source_id": source_id, "src": src, "dest": dest},
                    )

            for uid, snippet in loaded.snippets.items():
                snippet_id = uid.bytes
                cursor.execute(
                    sql("insert", "filetype"), {"filetype": snippet.filetype}
                )
                cursor.execute(
                    sql("insert", "snippet"),
                    {
                        "rowid": snippet_id,
                        "source_id": source_id,
                        "filetype": snippet.filetype,
                        "grammar": snippet.grammar.name,
                        "content": snippet.content,
                        "label": snippet.label,
                        "doc": snippet.doc,
                    },
                )
                for match in snippet.matches:
                    cursor.execute(
                        sql("insert", "match"),
                        {"snippet_id": snippet_id, "word": match},
                    )
            cursor.execute("PRAGMA optimize", ())

    def select(
        self, opts: MatchOptions, filetype: str, word: str, sym: str, limit: int
    ) -> Iterator[_Snip]:
        with suppress(OperationalError):
            with self._conn, closing(self._conn.cursor()) as cursor:
                cursor.execute(
                    sql("select", "snippets"),
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
                    yield cast(_Snip, row)
