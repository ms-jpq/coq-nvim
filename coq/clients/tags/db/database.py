from contextlib import closing, suppress
from hashlib import md5
from os.path import normcase
from pathlib import Path, PurePath
from sqlite3 import Connection, OperationalError
from typing import AbstractSet, Iterator, Mapping, cast

from pynvim_pp.lib import encode

from ....databases.types import DB
from ....shared.settings import MatchOptions
from ....shared.sql import init_db, like_esc
from ....tags.types import Tag, Tags
from .sql import sql

_SCHEMA = "v5"

_NIL_TAG = Tag(
    language="",
    path="",
    line=0,
    kind="",
    name="",
    pattern=None,
    typeref=None,
    scope=None,
    scopeKind=None,
    access=None,
)


def _init(db_dir: Path, cwd: PurePath) -> Connection:
    ncwd = normcase(cwd)
    name = f"{md5(encode(ncwd)).hexdigest()}-{_SCHEMA}"
    db = (db_dir / name).with_suffix(".sqlite3")
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = Connection(str(db), isolation_level=None)
    init_db(conn)
    conn.executescript(sql("create", "pragma"))
    conn.executescript(sql("create", "tables"))
    return conn


class CTDB(DB):
    def __init__(self, vars_dir: Path, cwd: PurePath) -> None:
        self._vars_dir = vars_dir / "clients" / "tags"
        self._conn = _init(self._vars_dir, cwd=cwd)

    def swap(self, cwd: PurePath) -> None:
        self._conn.close()
        self._conn = _init(self._vars_dir, cwd=cwd)

    def paths(self) -> Mapping[str, float]:
        with suppress(OperationalError):
            with self._conn, closing(self._conn.cursor()) as cursor:
                cursor.execute(sql("select", "files"), ())
                files = {row["filename"]: row["mtime"] for row in cursor.fetchall()}
                return files
        return {}

    def reconciliate(self, dead: AbstractSet[str], new: Tags) -> None:
        with suppress(OperationalError):
            with self._conn, closing(self._conn.cursor()) as cursor:

                def m1() -> Iterator[Mapping]:
                    for filename, (lang, mtime, _) in new.items():
                        yield {
                            "filename": filename,
                            "filetype": lang,
                            "mtime": mtime,
                        }

                def m2() -> Iterator[Mapping]:
                    for _, _, tags in new.values():
                        for tag in tags:
                            yield {**_NIL_TAG, **tag}

                cursor.executemany(
                    sql("delete", "file"),
                    ({"filename": f} for f in dead | new.keys()),
                )
                cursor.executemany(sql("insert", "file"), m1())
                cursor.executemany(sql("insert", "tag"), m2())
                cursor.execute("PRAGMA optimize", ())

    def select(
        self,
        opts: MatchOptions,
        filename: str,
        line_num: int,
        word: str,
        sym: str,
        limit: int,
    ) -> Iterator[Tag]:
        with suppress(OperationalError):
            with self._conn, closing(self._conn.cursor()) as cursor:
                cursor.execute(
                    sql("select", "tags"),
                    {
                        "cut_off": opts.fuzzy_cutoff,
                        "look_ahead": opts.look_ahead,
                        "limit": limit,
                        "filename": filename,
                        "line_num": line_num,
                        "word": word,
                        "sym": sym,
                        "like_word": like_esc(word[: opts.exact_matches]),
                        "like_sym": like_esc(sym[: opts.exact_matches]),
                    },
                )
                for row in cursor:
                    yield cast(Tag, {**row})
