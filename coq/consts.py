from os import environ, name
from os.path import normpath
from pathlib import Path

GIL_SWITCH = 1 / (10**3)
CACHE_CHUNK = 9

IS_WIN = name == "nt"

TOP_LEVEL = Path(__file__).resolve(strict=True).parent.parent
REQUIREMENTS = TOP_LEVEL / "requirements.txt"


VARS = TOP_LEVEL / ".vars"

RT_DIR = VARS / "runtime"
RT_PY = RT_DIR / "Scripts" / "python.exe" if IS_WIN else RT_DIR / "bin" / "python3"

_CONF_DIR = TOP_LEVEL / "config"
LANG_ROOT = TOP_LEVEL / "locale"
DEFAULT_LANG = "en"
_DOC_DIR = TOP_LEVEL / "docs"


CONFIG_YML = _CONF_DIR / "defaults.yml"
COMPILATION_YML = _CONF_DIR / "compilation.yml"


_ART_DIR = TOP_LEVEL / "artifacts"
HELO_ARTIFACTS = _ART_DIR / "helo.yml"


TMP_DIR = VARS / "tmp"


SETTINGS_VAR = "coq_settings"

REPL_GRAMMAR = environ.get("COQ_GRAMMAR", "lsp")

DEBUG = "COQ_DEBUG" in environ
DEBUG_METRICS = "COQ_DEBUG_METRICS" in environ
DEBUG_DB = "COQ_DEBUG_DB" in environ

BUFFER_DB = normpath(TMP_DIR / "buffers.sqlite3") if DEBUG_DB else ":memory:"
TREESITTER_DB = normpath(TMP_DIR / "treesitter.sqlite3") if DEBUG_DB else ":memory:"
INSERT_DB = normpath(TMP_DIR / "inserts.sqlite3") if DEBUG_DB else ":memory:"
TMUX_DB = normpath(TMP_DIR / "tmux.sqlite3") if DEBUG_DB else ":memory:"
REGISTER_DB = normpath(TMP_DIR / "register.sqlite3") if DEBUG_DB else ":memory:"


_URI_BASE = "https://github.com/ms-jpq/coq_nvim/tree/coq/docs/"

MD_README = _DOC_DIR / "README.md"
URI_README = _URI_BASE + MD_README.name

MD_CONF = _DOC_DIR / "CONF.md"
URI_CONF = _URI_BASE + MD_CONF.name

MD_KEYBIND = _DOC_DIR / "KEYBIND.md"
URI_KEYBIND = _URI_BASE + MD_KEYBIND.name

MD_SNIPS = _DOC_DIR / "SNIPS.md"
URI_SNIPS = _URI_BASE + MD_SNIPS.name

MD_FUZZY = _DOC_DIR / "FUZZY.md"
URI_FUZZY = _URI_BASE + MD_FUZZY.name

MD_DISPLAY = _DOC_DIR / "DISPLAY.md"
URI_DISPLAY = _URI_BASE + MD_DISPLAY.name

MD_SOURCES = _DOC_DIR / "SOURCES.md"
URI_SOURCES = _URI_BASE + MD_SOURCES.name

MD_MISC = _DOC_DIR / "MISC.md"
URI_MISC = _URI_BASE + MD_MISC.name

MD_STATS = _DOC_DIR / "STATS.md"
URI_STATISTICS = _URI_BASE + MD_STATS.name

MD_PREF = _DOC_DIR / "PERF.md"
URI_PREF = _URI_BASE + MD_PREF.name

MD_COMPLETION = _DOC_DIR / "COMPLETION.md"
URI_COMPLETION = _URI_BASE + MD_COMPLETION.name

MD_C_SOURCES = _DOC_DIR / "CUSTOM_SOURCES.md"
URI_C_SOURCES = _URI_BASE + MD_C_SOURCES.name
