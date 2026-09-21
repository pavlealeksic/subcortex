"""Make ``src/`` importable when running ``python3 -m unittest discover -s tests``
without installing the package."""

import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import os  # noqa: E402

# Hooks start a missing daemon by default; tests (and the subprocesses they
# spawn with a copy of this environment) must never do that.
os.environ["SUBCORTEX_AUTOSTART"] = "0"

# Never read the developer's real ~/.config/subcortex (config or secrets).
os.environ.setdefault("SUBCORTEX_CONFIG", "/nonexistent/subcortex-tests/config.json")

# Never write the developer's real ~/.local/share/subcortex either: an
# in-process daemon resolves data_dir() from this process's environment.
if not os.environ.get("SUBCORTEX_DATA_DIR"):
    import atexit  # noqa: E402
    import shutil  # noqa: E402
    import tempfile  # noqa: E402

    _DATA = tempfile.mkdtemp(prefix="subcortex-tests-")
    os.environ["SUBCORTEX_DATA_DIR"] = _DATA
    atexit.register(shutil.rmtree, _DATA, True)
