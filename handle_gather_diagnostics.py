#!/usr/bin/env python3
"""
handle_gather_diagnostics.py
Decrypts (.tgz.p7m) and/or extracts (.tgz) gather-diagnostics files.

Accepts bare folder names, .tgz, or .tgz.p7m as input.
For each input, if the exact path doesn't exist the script tries all
permutations automatically (.tgz.p7m → .tgz → folder).

Usage:
    python handle_gather_diagnostics.py <name> [name2] ...

Requires:
    - decrypt-cms.exe  (place in same directory as this script)
    - Vault/Microsoft SSO credentials (authenticated via device code flow on first use)
"""

import os
import subprocess
import sys
import tarfile
from pathlib import Path
from tkinter import Tk
from tkinter.filedialog import askopenfilenames

SCRIPT_DIR  = Path(__file__).parent
DECRYPT_CMS = SCRIPT_DIR / "decrypt-cms.exe"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def strip_extensions(p: Path) -> Path:
    """Return base path with any .p7m / .tgz / .tar.gz suffix stripped."""
    name = p.name
    if name.endswith(".p7m"):
        name = name[:-4]          # strip .p7m → may still end in .tgz or .tgz (N)
    if ".tgz" in name or ".tar.gz" in name:
        name = name[: name.find(".t")]    # strip from the first .tgz/.tar part
    return p.parent / name


def resolve(arg: str):
    """
    Given an input string, find what actually exists.
    Returns (path, kind) where kind is 'folder', 'tgz', or 'p7m'.
    Returns (None, None) if nothing is found.
    """
    p        = Path(arg)
    base     = strip_extensions(p)
    tgz      = Path(str(base) + ".tgz")
    p7m      = Path(str(base) + ".tgz.p7m")
    p7m_tgz  = Path(str(base) + ".tgz.p7m.tgz")

    # Check exact input first, then try permutations in order: folder → .tgz → .tgz.p7m → .tgz.p7m.tgz
    candidates = [p, base, tgz, p7m, p7m_tgz]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate, "folder"
        if candidate.exists():
            if candidate.name.endswith(".p7m"):
                return candidate, "p7m"
            if candidate.name.endswith(".tgz") or candidate.name.endswith(".tar.gz"):
                return candidate, "tgz"

    return None, None


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def decrypt(p7m_path: Path) -> Path:
    """Decrypt a .tgz.p7m file to .tgz using decrypt-cms.exe."""
    if not DECRYPT_CMS.exists():
        print(f"  [ERROR] decrypt-cms.exe not found at:\n         {DECRYPT_CMS}")
        sys.exit(1)

    tgz_path = p7m_path.parent / p7m_path.name[:-4]  # strip .p7m

    result = subprocess.run(
        [str(DECRYPT_CMS), str(p7m_path), str(tgz_path)]
    )

    if result.returncode != 0:
        sys.exit(1)

    return tgz_path


def extract(tgz_path: Path) -> Path:
    """Extract a .tgz archive into its parent directory."""
    dest = tgz_path.parent

    try:
        with tarfile.open(tgz_path, "r:gz") as tar:
            tar.extractall(dest)
    except Exception as e:
        print(f"[ERROR] Extraction failed: {e}")
        sys.exit(1)

    # Return the extracted item (folder or file) — strip .tgz extension
    extracted = strip_extensions(tgz_path)
    return extracted if extracted.exists() else dest


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def handle(arg: str) -> str | None:
    """Returns the final folder name on success, None on error.
    Chains automatically: .tgz.p7m.tgz → .tgz.p7m → .tgz → folder.
    """
    path, kind = resolve(arg)

    if path is None:
        return None

    if kind == "folder":
        return path.name

    current, current_kind = path, kind
    to_delete = set()  # intermediate files produced by the script; cleaned up after use

    while current_kind in ("p7m", "tgz"):
        if current_kind == "p7m":
            tgz_path = current.parent / current.name[:-4]  # strip .p7m
            if not tgz_path.exists():
                decrypt(current)
            if current in to_delete:
                current.unlink(missing_ok=True)
            current, current_kind = tgz_path, "tgz"

        elif current_kind == "tgz":
            # Final folder is named after everything before the first .tgz
            idx = current.name.find(".tgz")
            folder = current.parent / (current.name[:idx] if idx != -1 else current.name)
            if folder.is_dir():
                return folder.name
            extracted = extract(current)
            if extracted.is_dir():
                return extracted.name
            # Extracted a file (e.g. .p7m) — mark for cleanup, then keep chaining
            if extracted.name.endswith(".p7m"):
                to_delete.add(extracted)
            next_path, next_kind = resolve(str(extracted))
            if next_kind in ("p7m", "tgz"):
                current, current_kind = next_path, next_kind
            else:
                return extracted.name

    return current.name


def pick_files() -> list[str]:
    """Open a file picker dialog and return selected file paths."""
    root = Tk()
    root.withdraw()
    files = askopenfilenames(
        title="Select gather-diagnostics files",
        initialdir=os.getcwd(),
        filetypes=[("All files", "*.*")]
    )
    root.destroy()
    return list(files)


def clear_data_dir():
    """Delete all files in the data/ directory next to this script."""
    data_dir = SCRIPT_DIR / "data"
    if data_dir.is_dir():
        for f in data_dir.iterdir():
            if f.is_file():
                f.unlink()


def main():
    clear_data_dir()

    if len(sys.argv) < 2:
        args = pick_files()
        if not args:
            sys.exit(0)
    else:
        args = sys.argv[1:]

    processed = []
    errors = []
    for arg in args:
        result = handle(arg)
        if result:
            processed.append(result)
        else:
            errors.append(arg)

    if processed:
        print("\nExtracted:")
        for name in processed:
            print(f"  {name}")

    for arg in errors:
        print(f"\n[ERROR] Not found: {arg}")


if __name__ == "__main__":
    main()
