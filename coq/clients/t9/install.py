from asyncio import sleep
from contextlib import suppress
from io import BytesIO
from os import X_OK, access, sep
from os.path import normpath
from pathlib import Path, PurePath
from platform import uname
from shutil import move
from socket import timeout as TimeoutE
from string import Template
from tempfile import TemporaryDirectory
from typing import Callable, Mapping, Optional, Tuple
from urllib.error import URLError
from zipfile import ZipFile

from pynvim_pp.lib import decode
from pynvim_pp.logging import log
from std2.asyncio import to_thread
from std2.platform import OS, os
from std2.urllib import urlopen

_VER = "https://update.tabnine.com/bundles/version"
_DOWN = Template("https://update.tabnine.com/bundles/$version/$triple/TabNine.zip")
_T9_EXEC = PurePath("TabNine").with_suffix(".exe" if os is OS.windows else "")
_X_MOD = 0o755


def _is_linux_musl() -> bool:
    path = Path(sep) / "etc" / "os-release"
    with suppress(OSError):
        with path.open("rb") as io:
            for line in io:
                if b"alpine" in line:
                    return True

    return False


def _triple() -> Optional[str]:
    u = uname()
    triples: Mapping[Tuple[str, str], Callable[[], Optional[str]]] = {
        ("arm64", "darwin"): lambda: "aarch64-apple-darwin",
        ("x86_64", "darwin"): lambda: "x86_64-apple-darwin",
        ("aarch64", "linux"): lambda: None,
        ("x86_64", "linux"): lambda: (
            "x86_64-unknown-linux-musl"
            if _is_linux_musl()
            else "x86_64-unknown-linux-gnu"
        ),
        ("amd64", "windows"): lambda: "x86_64-pc-windows-gnu",
    }
    if triple := triples.get((u.machine.casefold(), u.system.casefold())):
        return triple()
    else:
        return None


def _version(timeout: float) -> str:
    with urlopen(_VER, timeout=timeout) as resp:
        return decode(resp.read())


def _uri(timeout: float) -> Optional[str]:
    if triple := _triple():
        ver = _version(timeout)
        uri = _DOWN.substitute(version=ver, triple=triple)
        return uri
    else:
        return None


def t9_bin(vars_dir: Path) -> Path:
    return vars_dir / _T9_EXEC


def x_ok(vars_dir: Path) -> bool:
    try:
        paths = tuple(vars_dir.iterdir())
        ok = bool(paths) and all(access(path, X_OK) for path in paths)
    except OSError:
        return False
    else:
        return ok


def _update(vars_dir: Path, timeout: float) -> bool:
    vars_dir.mkdir(parents=True, exist_ok=True)
    lock = vars_dir / "versions.lock"

    try:
        p_uri = lock.read_text()
    except FileNotFoundError:
        p_uri = ""

    uri = _uri(timeout)
    if not uri:
        return False
    else:
        if not x_ok(vars_dir) or uri != p_uri:
            with urlopen(uri, timeout=timeout) as resp:
                buf = BytesIO(resp.read())
                with TemporaryDirectory(dir=vars_dir) as tmp:
                    with ZipFile(buf) as zip:
                        zip.extractall(path=tmp)
                    for child in Path(tmp).iterdir():
                        child.chmod(_X_MOD)
                        move(normpath(child), vars_dir / child.name)

            lock.write_text(uri)
            lock.chmod(_X_MOD)

        return True


async def ensure_updated(
    vars_dir: Path, retries: int, timeout: float
) -> Optional[PurePath]:
    bin = t9_bin(vars_dir)
    for _ in range(retries):
        try:
            cont = await to_thread(_update, vars_dir=vars_dir, timeout=timeout)
        except (URLError, TimeoutE) as e:
            log.warning("%s", e)
            await sleep(timeout)
        else:
            if not cont:
                return None
            elif access(bin, X_OK):
                return bin
    else:
        return None
