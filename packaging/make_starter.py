"""Package a trained brain as the pre-trained "Starter" that ships inside the app.

    python packaging/make_starter.py <path to a trained brain folder>

Keeps generation 0 and the newest generation only (so Before / after works), the progress history,
level and events, and only the Hall of Fame records whose generations are kept. Writes snekbrain/starter/starter/.
"""
from __future__ import annotations

import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "snekbrain" / "starter" / "starter"


def copy_checkpoint(src: Path, dst: Path):
    """Copy a checkpoint, dropping Stable-Baselines3's note about the machine it was trained on."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "system_info.txt":
                data = b"- Packaged by SnekBrain\n"
            zout.writestr(item, data)


def main():
    src = Path(sys.argv[1]).resolve()
    gens = sorted(int(p.stem.split("_")[1]) for p in (src / "generations").glob("gen_*.zip"))
    if len(gens) < 2:
        raise SystemExit("needs at least two saved generations")
    keep = sorted({gens[0], gens[-1]})
    shutil.rmtree(DEST, ignore_errors=True)
    (DEST / "generations").mkdir(parents=True)
    for g in keep:
        copy_checkpoint(src / "generations" / f"gen_{g:04d}.zip", DEST / "generations" / f"gen_{g:04d}.zip")
    meta = json.loads((src / "brain.json").read_text(encoding="utf-8"))
    meta.update({"name": "Starter", "parent": None, "preset": meta.get("preset"),
                 "notes": f"Pre-trained for {gens[-1]} generations before SnekBrain shipped, so you can see a skilled "
                          "snake right away. Select it to watch, branch it, or keep training it."})
    (DEST / "brain.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    for name in ("progress.csv", "progress_state.json", "events.jsonl"):
        if (src / name).exists():
            shutil.copy2(src / name, DEST / name)
    hall = json.loads((src / "hall.json").read_text(encoding="utf-8")) if (src / "hall.json").exists() else {}
    hall["records"] = [r for r in hall.get("records", []) if r.get("generation") in keep]
    (DEST / "hall.json").write_text(json.dumps(hall, indent=1), encoding="utf-8")
    size = sum(p.stat().st_size for p in DEST.rglob("*") if p.is_file())
    print(f"Starter: generations {keep}, {size / 1e6:.1f} MB -> {DEST}")


if __name__ == "__main__":
    main()
