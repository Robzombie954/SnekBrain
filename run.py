"""Run SnekBrain from a source checkout: python run.py  (or --background / --quit)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from snekbrain.app import main  # noqa: E402

if __name__ == "__main__":
    main()
