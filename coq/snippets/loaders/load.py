from dataclasses import asdict
from os.path import normcase
from pathlib import Path
from typing import AbstractSet, Callable, Iterable, Iterator, MutableMapping, MutableSet
from uuid import UUID, uuid3

from pynvim_pp.logging import log
from std2.graphlib import recur_sort
from std2.pathlib import walk

from ...shared.types import UTF8, SnippetGrammar
from ..types import LoadedSnips, LoadError, ParsedSnippet
from .lsp import load_lsp
from .neosnippet import load_neosnippet
from .ultisnip import load_ultisnip


def _load_paths(search: Iterable[Path], exts: AbstractSet[str]) -> Iterator[Path]:
    for search_path in search:
        for path in walk(search_path):
            if path.suffix in exts:
                yield Path(normcase(path))


def _key(snip: ParsedSnippet) -> UUID:
    name = str(recur_sort(asdict(snip)))
    return uuid3(UUID(int=0), name=name)


def load_direct(
    trans: Callable[[ParsedSnippet], ParsedSnippet],
    ignore_error: bool,
    lsp: Iterable[Path],
    neosnippet: Iterable[Path],
    ultisnip: Iterable[Path],
    lsp_grammar: SnippetGrammar = SnippetGrammar.lsp,
    neosnippet_grammar: SnippetGrammar = SnippetGrammar.snu,
    ultisnip_grammar: SnippetGrammar = SnippetGrammar.snu,
) -> LoadedSnips:
    specs = {
        load_lsp: (lsp_grammar, lsp),
        load_neosnippet: (neosnippet_grammar, neosnippet),
        load_ultisnip: (ultisnip_grammar, ultisnip),
    }

    extensions: MutableMapping[str, MutableSet[str]] = {}
    snippets: MutableMapping[UUID, ParsedSnippet] = {}

    for parser, (grammar, paths) in specs.items():
        for path in paths:
            with path.open(encoding=UTF8) as fd:
                try:
                    filetype, exts, snips = parser(
                        grammar, path=path, lines=enumerate(fd, start=1)
                    )
                except LoadError as e:
                    if ignore_error:
                        log.warning("%s", e)
                    else:
                        raise
                else:
                    ext_acc = extensions.setdefault(filetype, set())
                    for ext in exts:
                        ext_acc.add(ext)
                    for snip in map(trans, snips):
                        uid = _key(snip)
                        snippets[uid] = snip

    loaded = LoadedSnips(exts=extensions, snippets=snippets)
    return loaded


def load_ci(
    trans: Callable[[ParsedSnippet], ParsedSnippet],
    lsp: Iterable[Path],
    neosnippet: Iterable[Path],
    ultisnip: Iterable[Path],
) -> LoadedSnips:
    loaded = load_direct(
        trans,
        True,
        lsp=_load_paths(lsp, exts={".json"}),
        neosnippet=_load_paths(neosnippet, exts={".snippets", ".snip"}),
        ultisnip=_load_paths(ultisnip, exts={".snippets", ".snip"}),
    )

    return loaded
