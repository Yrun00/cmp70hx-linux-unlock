#!/usr/bin/env python3
"""Create source ZIP, tar.gz and self-extracting Python zipapp; no vendor binaries."""
import hashlib
import gzip
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent
NAME = "cmp70hx-linux-0.2.6"
TOP = {
    "cmp70hx.py", "cmp_mme_manage.py", "test_kit.py", "test_install.py", "test_cmp_mme_manage.py",
    "package.py", "README.md", "LICENSE", "THIRD_PARTY.md", "PROVENANCE.json",
    "cmp70hx_wizard.py", "cmp70hx_boot.py", "test_wizard.py", "test_boot.py",
    ".gitignore", ".gitattributes", "test_release.py",
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


def write_zip(path, entries, prefix=b""):
    """Use fixed metadata so identical source bytes produce identical archives."""
    with path.open("wb") as output:
        output.write(prefix)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in sorted(entries.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)


def main():
    target = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT.parent / "dist").resolve()
    target.mkdir(parents=True, exist_ok=True)
    artifacts = [target / (NAME + extension) for extension in (".zip", ".tar.gz", ".pyz")]
    if any(path.exists() for path in artifacts):
        raise SystemExit("Release output already exists; choose a new output directory")
    files = list(source_files())
    contents = {p.relative_to(ROOT).as_posix(): p.read_bytes() for p in files}
    if any(b"\r\n" in data for data in contents.values()):
        raise SystemExit("Source files must use LF line endings; use a fresh Git checkout with .gitattributes")
    hashes = "".join(hashlib.sha256(data).hexdigest() + "  " + name + "\n" for name, data in sorted(contents.items()))
    contents["SHA256SUMS"] = hashes.encode("utf-8")
    archive_contents = {NAME + "/" + name: data for name, data in contents.items()}
    with tempfile.TemporaryDirectory() as temp:
        write_zip(artifacts[0], archive_contents)
        with artifacts[1].open("wb") as output:
            with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                    for name, data in sorted(archive_contents.items()):
                        info = tarfile.TarInfo(name)
                        info.size = len(data)
                        info.mode = 0o644
                        archive.addfile(info, io.BytesIO(data))
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
        write_zip(artifacts[2], {p.name: p.read_bytes() for p in app.iterdir()},
                  prefix=b"#!/usr/bin/env python3\n")
    (target / "SHA256SUMS").write_text("".join(hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.name + "\n" for p in artifacts),
                                      encoding="utf-8", newline="\n")
    print(json.dumps(dict(files=len(files), artifacts=[str(p) for p in artifacts]), indent=2))


if __name__ == "__main__":
    main()
