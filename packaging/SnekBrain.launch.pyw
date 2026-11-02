"""Starts Terrelbytes SnekBrain from its install folder (what the Start menu and desktop shortcuts run).

Pass --background to start the engine only, or --quit to save and stop a running copy.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKGS = HERE / "pkgs"
if str(PKGS) not in sys.path:
    sys.path.insert(0, str(PKGS))

if sys.stdout is None or "pythonw" in Path(sys.executable).name.lower():
    # No console: keep any stray output in the app's own data folder, never next to other apps' files.
    home = Path(os.environ.get("SNEKBRAIN_HOME") or Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "SnekBrain")
    try:
        home.mkdir(parents=True, exist_ok=True)
        logf = home / "launcher.log"
        if logf.exists() and logf.stat().st_size > 1_000_000:
            logf.unlink()
        log = open(logf, "a", encoding="utf-8", errors="replace")  # noqa: SIM115
        sys.stdout = sys.stderr = log
    except OSError:
        pass

from snekbrain.app import main  # noqa: E402

if __name__ == "__main__":
    main()
