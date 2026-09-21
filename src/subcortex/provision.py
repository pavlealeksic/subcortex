"""Backend environment provisioning for setup.

The laya backend needs a heavy package (``laya-mlx`` on Apple Silicon, ``laya``
elsewhere) next to subcortex itself. By default setup gives the daemon a
dedicated virtualenv at ``~/.local/share/subcortex/venv`` holding both, so the
user's own Python environments are never modified. Everything here reports
problems as return values; nothing raises on expected failures.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import __version__
from .config import DATA_DIR, VENV_PYTHON

VENV_DIR = DATA_DIR / "venv"


def apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def laya_package() -> str:
    return "laya-mlx" if apple_silicon() else "laya"


def laya_module() -> str:
    return "laya_mlx" if apple_silicon() else "laya"


def daemon_python() -> str:
    """The interpreter the daemon runs under: ``daemon_python`` from config (set by
    setup), else the dedicated venv if present, else this interpreter."""
    from .config import load_config

    chosen = str(load_config().get("daemon_python") or "").strip()
    if chosen and os.access(chosen, os.X_OK):
        return chosen
    return str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable


def daemon_argv(python: Optional[str] = None) -> List[str]:
    """How to start the daemon. Isolated (``-I``): PYTHONPATH and friends are
    ignored and the working directory never lands on sys.path, so a project's
    own ``json.py`` can't break the daemon or run inside it. Callers also start
    it from the data dir, never the user's project."""
    python = python or daemon_python()
    root = source_checkout()
    if root:  # dev checkout: run this code, whatever the interpreter
        boot = (f"import sys; sys.path.insert(0, {str(root / 'src')!r}); "
                "from subcortex.cli import main; sys.exit(main(['serve', '--foreground']))")
        return [python, "-I", "-c", boot]
    return [python, "-I", "-m", "subcortex", "serve", "--foreground"]


def source_checkout() -> Optional[Path]:
    """Repo root when subcortex runs from a source checkout (dev / editable install)."""
    root = Path(__file__).resolve().parents[2]
    pyproject = root / "pyproject.toml"
    try:
        if pyproject.is_file() and 'name = "subcortex"' in pyproject.read_text():
            return root
    except OSError:
        pass
    return None


def subcortex_requirement() -> List[str]:
    """pip arguments that install *this* subcortex into another environment:
    the same source checkout, git commit or archive it was installed from
    (PEP 610 ``direct_url.json``), else this version from PyPI."""
    root = source_checkout()
    if root:
        return ["-e", str(root)]
    try:
        from importlib.metadata import distribution

        direct = json.loads(distribution("subcortex").read_text("direct_url.json") or "null")
    except Exception:
        direct = None
    if isinstance(direct, dict) and isinstance(direct.get("url"), str):
        url = direct["url"]
        vcs = direct.get("vcs_info") or {}
        if vcs.get("vcs") == "git":
            return [f"git+{url}@{vcs.get('commit_id') or vcs.get('requested_revision') or 'HEAD'}"]
        if (direct.get("dir_info") or {}).get("editable") and url.startswith("file://"):
            return ["-e", url[len("file://"):]]
        return [url]
    return [f"subcortex=={__version__}"]


def probe(python: str) -> Dict[str, Optional[str]]:
    """What ``python`` can import: {"python": version, "subcortex": version|None, "<laya module>": "yes"|None}."""
    code = (
        "import importlib.util, json, sys\n"
        "out = {'python': sys.version.split()[0]}\n"
        "try:\n"
        "    import subcortex; out['subcortex'] = subcortex.__version__\n"
        "except Exception:\n"
        "    out['subcortex'] = None\n"
        f"out['{laya_module()}'] = 'yes' if importlib.util.find_spec('{laya_module()}') else None\n"
        "print(json.dumps(out))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    try:
        proc = subprocess.run([python, "-c", code], capture_output=True, text=True, timeout=60, env=env)
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        return {"python": None, "subcortex": None, laya_module(): None}


def backend_ready(python: str) -> Tuple[bool, str]:
    info = probe(python)
    if not info.get("python"):
        return False, f"{python} does not run"
    if not info.get(laya_module()):
        return False, f"{laya_package()} is not installed for {python}"
    if info.get("subcortex") != __version__:
        found = info.get("subcortex") or "not installed"
        return False, f"subcortex there is {found}, this is {__version__}"
    return True, f"{laya_package()} and subcortex {__version__} ready in {python}"


def venv_is_foreign() -> Optional[str]:
    """If the venv path is a symlink (e.g. into another project's env), its target."""
    if VENV_DIR.is_symlink():
        return os.readlink(VENV_DIR)
    return None


def create_venv(replace: bool = False) -> Tuple[bool, str]:
    """Create the dedicated venv. ``replace`` drops a symlink/broken env first —
    a symlink is only unlinked, never followed."""
    try:
        if VENV_DIR.is_symlink():
            if not replace:
                return False, f"{VENV_DIR} is a symlink to {os.readlink(VENV_DIR)}"
            VENV_DIR.unlink()
        elif VENV_DIR.exists() and replace:
            shutil.rmtree(VENV_DIR)
        if not VENV_PYTHON.exists():
            VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True,
                           capture_output=True, text=True, timeout=300)
        return True, str(VENV_DIR)
    except subprocess.CalledProcessError as exc:
        return False, (exc.stderr or exc.stdout or str(exc)).strip()[-500:]
    except Exception as exc:
        return False, str(exc)


def pip_install(python: str, args: List[str],
                on_line: Optional[Callable[[str], None]] = None) -> Tuple[bool, str]:
    """``python -m pip install <args>``; returns (ok, last lines of output)."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    cmd = [python, "-m", "pip", "install", "--disable-pip-version-check", *args]
    has_pip = subprocess.run([python, "-m", "pip", "--version"], capture_output=True, env=env).returncode == 0
    if not has_pip and shutil.which("uv"):  # uv-managed environments ship without pip
        cmd = ["uv", "pip", "install", "--python", python, *args]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=env)
    except OSError as exc:
        return False, str(exc)
    tail: List[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        tail = (tail + [line.rstrip()])[-15:]
        if on_line:
            on_line(line.rstrip())
    return proc.wait() == 0, "\n".join(tail)


def install_laya(python: str, on_line: Optional[Callable[[str], None]] = None) -> Tuple[bool, str]:
    """Install the laya package and this subcortex into ``python``'s environment."""
    return pip_install(python, [laya_package(), *subcortex_requirement()], on_line)
