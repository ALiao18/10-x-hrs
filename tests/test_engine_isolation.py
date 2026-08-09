"""The engine must stay importable and usable with no Textual present.

This is a stated design constraint rather than a behaviour, so it gets its own
guard: a stray `from textual...` in an engine module is easy to add and
impossible to notice until the headless CLI or a test breaks.
"""

import subprocess
import sys
from pathlib import Path

ENGINE = ("models", "ids", "store", "parse", "stats", "sync", "config")
SRC = Path(__file__).resolve().parents[1] / "src" / "tenx"


def test_no_engine_module_imports_textual():
    offenders = [name for name in ENGINE if "textual" in (SRC / f"{name}.py").read_text(encoding="utf-8")]
    assert offenders == [], f"engine modules must not reference textual: {offenders}"


def test_engine_imports_with_textual_blocked():
    """Import the engine in a subprocess where `textual` raises on import."""
    script = (
        "import builtins, sys\n"
        "real = builtins.__import__\n"
        "def guard(name, *a, **k):\n"
        "    if name == 'textual' or name.startswith('textual.'):\n"
        "        raise ImportError('textual is not installed')\n"
        "    return real(name, *a, **k)\n"
        "builtins.__import__ = guard\n"
        "import tenx.store, tenx.stats, tenx.parse, tenx.sync, tenx.config, tenx.ids\n"
        "print('ok')\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout
