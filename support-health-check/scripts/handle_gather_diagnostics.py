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
    """Return base path with any .p7m / .tgz / .tar.gz / .tar suffix stripped."""
    name = p.name
    if name.endswith(".p7m"):
        name = name[:-4]          # strip .p7m → may still end in .tgz or .tgz (N)
    if ".tgz" in name or ".tar.gz" in name:
        name = name[: name.find(".t")]    # strip from the first .tgz/.tar part
    elif name.endswith(".tar"):
        name = name[:-4]          # strip plain .tar
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
    tar_gz   = Path(str(base) + ".tar.gz")
    tar      = Path(str(base) + ".tar")
    p7m      = Path(str(base) + ".tgz.p7m")
    p7m_tgz  = Path(str(base) + ".tgz.p7m.tgz")

    # Check exact input first, then try permutations in order:
    # folder → .tgz → .tar.gz → .tar → .tgz.p7m → .tgz.p7m.tgz
    # Also try appending .p7m to the exact input — handles names like "file.tgz (1)" → "file.tgz (1).p7m"
    p_p7m = Path(str(p) + ".p7m")
    candidates = [p, p_p7m, base, tgz, tar_gz, tar, p7m, p7m_tgz]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate, "folder"
        if candidate.exists():
            if candidate.name.endswith(".p7m"):
                return candidate, "p7m"
            if candidate.name.endswith(".tgz") or candidate.name.endswith(".tar.gz") or candidate.name.endswith(".tar"):
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
    """Extract a .tgz, .tar.gz, or .tar archive into its parent directory."""
    dest = tgz_path.parent

    try:
        with tarfile.open(tgz_path, "r:*") as tar:
            top_names = {m.name.split("/")[0] for m in tar.getmembers() if m.name and m.name != "."}
            tar.extractall(dest)
    except Exception as e:
        print(f"[ERROR] Extraction failed: {e}")
        sys.exit(1)

    # Prefer exact strip_extensions match (common case)
    expected = strip_extensions(tgz_path)
    if expected.exists():
        return expected

    # Fall back to inspecting what was actually at the top level of the archive
    top_items = [dest / name for name in top_names if (dest / name).exists()]
    if len(top_items) == 1:
        return top_items[0]

    # Multiple or unknown top-level items — return dest as container
    return dest


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
            # Final folder is named after everything before the first tar extension
            idx = current.name.find(".tgz")
            if idx == -1:
                idx = current.name.find(".tar")
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


def auto_discover_gd(search_dir: Path) -> list[str]:
    """
    Auto-discover gather-diagnostics artifacts in a directory.
    Mirrors the full set of formats that resolve() handles, in priority order:
    .tgz.p7m.tgz > .tgz.p7m > .tgz / .tar.gz / .tar > extracted folder.
    Returns a deduplicated list (one entry per base name, most-raw form wins).
    """
    candidates = {}  # base_name -> (priority, path)
    for p in search_dir.glob("gather-diagnostics*.tgz.p7m.tgz"):
        base = strip_extensions(p).name
        candidates[base] = (0, str(p))
    for p in search_dir.glob("gather-diagnostics*.tgz.p7m"):
        base = strip_extensions(p).name
        if base not in candidates:
            candidates[base] = (1, str(p))
    for p in search_dir.glob("gather-diagnostics*.tgz"):
        base = strip_extensions(p).name
        if base not in candidates:
            candidates[base] = (2, str(p))
    for p in search_dir.glob("gather-diagnostics*.tar.gz"):
        base = strip_extensions(p).name
        if base not in candidates:
            candidates[base] = (2, str(p))
    for p in search_dir.glob("gather-diagnostics*.tar"):
        if not p.name.endswith(".tar.gz"):
            base = strip_extensions(p).name
            if base not in candidates:
                candidates[base] = (2, str(p))
    for p in search_dir.iterdir():
        if p.is_dir() and p.name.startswith("gather-diagnostics"):
            base = strip_extensions(p).name
            if base not in candidates:
                candidates[base] = (3, str(p))
    return [path for _, (_, path) in sorted(candidates.items())]


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


def recombine_args(raw: list[str]) -> list[str]:
    """
    Recombine filename parts that were split by the shell on spaces.
    e.g. ['file.tgz', '(1)', 'other.tgz', '(1)'] -> ['file.tgz (1)', 'other.tgz (1)']
    Handles bare '(N)' or '(N).p7m' suffixes appended by Windows when downloading
    duplicate files.
    """
    import re
    result = []
    for arg in raw:
        if re.match(r'^\(\d+\)(\.\w+)*$', arg) and result:
            result[-1] = result[-1] + ' ' + arg
        else:
            result.append(arg)
    return result


def main():
    clear_data_dir()

    if len(sys.argv) < 2:
        args = auto_discover_gd(Path.cwd())
        if not args:
            if os.environ.get("DISPLAY") or sys.platform == "win32":
                args = pick_files()
            if not args:
                print("[ERROR] No gather-diagnostics files found in current directory.")
                print("  Provide paths as arguments, or run from a directory containing gather-diagnostics files.")
                sys.exit(1)
    else:
        args = recombine_args(sys.argv[1:])

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
