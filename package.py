#!/usr/bin/env python3
"""Create source ZIP, tar.gz and self-extracting Python zipapp; no vendor binaries."""
import hashlib
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import zipfile
import zipapp

ROOT = Path(__file__).resolve().parent
NAME = "cmp70hx-linux-0.2.5"
TOP = {
    "cmp70hx.py", "cmp_mme_manage.py", "test_kit.py", "test_install.py", "test_cmp_mme_manage.py",
    "package.py", "README.md", "LICENSE", "THIRD_PARTY.md", "PROVENANCE.json",
    "cmp70hx_wizard.py", "cmp70hx_boot.py", "test_wizard.py", "test_boot.py",
    ".gitignore", "test_release.py",
}
DOCUMENTS = {"BEGINNER.md", "MANUAL.md", "RECOVERY.md", "VALIDATION.md", "DEVELOPMENT.md"}


def source_files():
    provenance = json.loads((ROOT / "PROVENANCE.json").read_text())
    names = TOP | {"docs/" + name for name in DOCUMENTS} | {
        ".github/workflows/ci.yml", "LICENSES/GPL-2.0-only.txt", "LICENSES/pearlfortune-MIT.txt"}
    names |= {entry["file"] for entry in provenance["files"]}
    for name in sorted(names):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("Invalid package path: " + name)
        path = ROOT / relative
        if not path.is_file() or any(p.is_symlink() for p in [path, *path.parents] if p != ROOT and ROOT in p.parents):
            raise RuntimeError("Missing or symlinked package source: " + name)
        yield path


def main():
    target = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT.parent / "dist").resolve()
    target.mkdir(parents=True, exist_ok=True)
    artifacts = [target / (NAME + extension) for extension in (".zip", ".tar.gz", ".pyz")]
    if any(path.exists() for path in artifacts):
        raise SystemExit("Release output already exists; choose a new output directory")
    files = list(source_files())
    hashes = "".join(hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.relative_to(ROOT).as_posix() + "\n" for p in files)
    with tempfile.TemporaryDirectory() as temp:
        stage = Path(temp) / NAME
        stage.mkdir()
        for path in files:
            dest = stage / path.relative_to(ROOT)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(path.read_bytes())
        (stage / "SHA256SUMS").write_text(hashes, encoding="utf-8", newline="\n")
        with zipfile.ZipFile(artifacts[0], "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for p in sorted(stage.rglob("*")):
                if p.is_file():
                    archive.write(p, NAME + "/" + p.relative_to(stage).as_posix())
        with tarfile.open(artifacts[1], "w:gz") as archive:
            for p in sorted(stage.rglob("*")):
                if p.is_file():
                    archive.add(p, arcname=NAME + "/" + p.relative_to(stage).as_posix())
        app = Path(temp) / "app"
        app.mkdir()
        (app / "payload.zip").write_bytes(artifacts[0].read_bytes())
        (app / "__main__.py").write_text('''import hashlib, pathlib, runpy, sys, tempfile, zipfile
with tempfile.TemporaryDirectory(prefix="cmp70hx-kit-") as tmp:
    with zipfile.ZipFile(sys.argv[0]) as outer:
        payload = pathlib.Path(tmp) / "payload.zip"
        payload.write_bytes(outer.read("payload.zip"))
    with zipfile.ZipFile(payload) as archive:
        archive.extractall(tmp)
    root = pathlib.Path(tmp) / "''' + NAME + '''"
    for line in (root / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
            raise SystemExit("Embedded source hash mismatch")
    sys.path.insert(0, str(root))
    runpy.run_path(str(root / "cmp70hx.py"), run_name="__main__")
''', encoding="utf-8", newline="\n")
        zipapp.create_archive(app, artifacts[2], interpreter="/usr/bin/env python3", compressed=True)
    (target / "SHA256SUMS").write_text("".join(hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.name + "\n" for p in artifacts),
                                      encoding="utf-8", newline="\n")
    print(json.dumps(dict(files=len(files), artifacts=[str(p) for p in artifacts]), indent=2))


if __name__ == "__main__":
    main()
