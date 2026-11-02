"""Build the Terrelbytes SnekBrain Windows installer (SnekBrain-<version>-setup.exe).

Run on Windows with Python 3.13 64-bit and NSIS 3 installed:

    python -m pip install pynsist==2.8
    python packaging/build_installer.py

Steps: build the snekbrain wheel and every dependency wheel for Windows, let pynsist lay out the
install folder (embedded Python + pkgs), trim files only needed to compile PyTorch extensions, add the
Microsoft C++ runtime DLLs PyTorch needs (app-local, so PCs without the VC++ redistributable work),
then run makensis and write SHA256SUMS.txt next to the installer.
"""
from __future__ import annotations

import argparse
import hashlib
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "packaging"
BUILD = ROOT / "build"
WHEELS = BUILD / "wheels"
NSIS_DIR = BUILD / "nsis"

# PyTorch's DLLs import these from the Microsoft Visual C++ runtime. The embeddable Python only ships
# vcruntime140*.dll, so all of them are copied from this PC's System32 together (one matching set).
VC_DLLS = ["msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "msvcp140_atomic_wait.dll",
           "msvcp140_codecvt_ids.dll", "vcruntime140.dll", "vcruntime140_1.dll", "vcruntime140_threads.dll",
           "concrt140.dll"]
VC_REQUIRED = {"msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll", "vcruntime140_threads.dll"}


def version() -> str:
    text = (ROOT / "snekbrain" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)


def run(cmd: list[str]):
    print(">", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def build_wheels():
    shutil.rmtree(WHEELS, ignore_errors=True)
    WHEELS.mkdir(parents=True)
    pip = [sys.executable, "-m", "pip", "wheel", "--disable-pip-version-check", "--only-binary=:all:", "-w", str(WHEELS)]
    run([sys.executable, "-m", "pip", "wheel", "--disable-pip-version-check", "--no-deps", "-w", str(WHEELS), str(ROOT)])
    run(pip + ["-r", str(PKG / "requirements-win.txt")])
    run(pip + ["--no-deps", "-r", str(PKG / "requirements-nodeps.txt")])


def write_cfg(py_version: str, ver: str) -> Path:
    wheels = "\n    ".join(str(p) for p in sorted(WHEELS.glob("*.whl")))
    cfg = f"""[Application]
name=Terrelbytes SnekBrain
version={ver}
publisher=Terrelbytes AI Solutions
script=SnekBrain.launch.pyw
icon={PKG / 'snekbrain.ico'}
license_file={ROOT / 'LICENSE'}

[Python]
version={py_version}
bitness=64
include_msvcrt=false

[Include]
local_wheels={wheels}

[Build]
directory={NSIS_DIR}
installer_name=SnekBrain-{ver}-setup.exe
nsi_template=installer_template.nsi
"""
    # pynsist looks the template up relative to the config file's folder, so it sits next to it
    shutil.copy2(PKG / "installer.nsi", BUILD / "installer_template.nsi")
    # and the launcher too: pynsist builds the shortcut's argument from this path, so it must be a bare name
    shutil.copy2(PKG / "SnekBrain.launch.pyw", BUILD / "SnekBrain.launch.pyw")
    path = BUILD / "installer.cfg"
    path.write_text(cfg, encoding="utf-8")
    return path


def trim(pkgs: Path):
    """Drop what is only used to compile C++ extensions against PyTorch (about 85 MB)."""
    shutil.rmtree(pkgs / "torch" / "include", ignore_errors=True)
    for lib in (pkgs / "torch" / "lib").glob("*.lib"):
        lib.unlink()


def add_vc_runtime(python_dir: Path):
    sys32 = Path(r"C:\Windows\System32")
    copied = []
    for name in VC_DLLS:
        src = sys32 / name
        if src.exists():
            shutil.copy2(src, python_dir / name)
            copied.append(name)
    missing = VC_REQUIRED - set(copied)
    if missing:
        raise SystemExit(f"Missing Visual C++ runtime DLLs on this PC: {sorted(missing)}. "
                         "Install the latest Microsoft Visual C++ Redistributable (x64) and build again.")
    print("Added C++ runtime:", ", ".join(copied))


def find_makensis() -> str:
    exe = shutil.which("makensis")
    if exe:
        return exe
    for base in (r"C:\Program Files (x86)\NSIS", r"C:\Program Files\NSIS"):
        if Path(base, "makensis.exe").exists():
            return str(Path(base, "makensis.exe"))
    raise SystemExit("makensis not found: install NSIS 3 (https://nsis.sourceforge.io).")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--python", default=platform.python_version(),
                    help="embedded Python version (default: the one running this script)")
    ap.add_argument("--skip-wheels", action="store_true", help="reuse build/wheels")
    args = ap.parse_args()
    if sys.platform != "win32" or platform.architecture()[0] != "64bit":
        raise SystemExit("Build the installer on 64-bit Windows.")
    if not args.python.startswith("3.13."):
        raise SystemExit(f"Use Python 3.13 to build (the requirements are pinned for it), not {args.python}.")
    ver = version()
    BUILD.mkdir(exist_ok=True)
    if not args.skip_wheels:
        build_wheels()
    cfg = write_cfg(args.python, ver)
    run([sys.executable, "-m", "nsist", "--no-makensis", str(cfg)])
    trim(NSIS_DIR / "pkgs")
    add_vc_runtime(NSIS_DIR / "Python")
    run([find_makensis(), "/V2", str(NSIS_DIR / "installer.nsi")])
    exe = NSIS_DIR / f"SnekBrain-{ver}-setup.exe"
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    shutil.copy2(exe, dist / exe.name)
    (dist / "SHA256SUMS.txt").write_text(f"{sha256(dist / exe.name)}  {exe.name}\n", encoding="utf-8")
    print(f"Built {dist / exe.name} ({(dist / exe.name).stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
