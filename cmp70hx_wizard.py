"""Standalone interactive frontend. No application or service management."""
from __future__ import annotations

import argparse
import json
import os
import queue
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import threading
import urllib.request

import cmp70hx as kit
import cmp70hx_boot as boot_setup

SOURCE_NAME = f"NVIDIA-kernel-module-source-{kit.VERSION}.tar.xz"
SOURCE_URL = "https://download.nvidia.com/XFree86/NVIDIA-kernel-module-source/" + SOURCE_NAME


def ask(prompt, default=None):
    value = input(prompt + (f" [{default}]" if default is not None else "") + ": ").strip()
    return value or (default if default is not None else "")


def yes(prompt, default=False):
    while True:
        value = ask(prompt + " (yes/no)", "yes" if default else "no").lower()
        if value in ("yes", "y"): return True
        if value in ("no", "n"): return False
        print("Enter yes or no.")


def numbers(value, count, allow_empty=False):
    if value.lower() == "none" and allow_empty: return []
    if value.lower() == "all": return list(range(count))
    parts = value.replace(" ", "").split(",")
    kit.require(all(re.fullmatch(r"[0-9]+", x) for x in parts), "Use numbers separated by commas, all, or none")
    result = [int(x) - 1 for x in parts]
    kit.require(len(set(result)) == len(result) and all(0 <= x < count for x in result),
                "Choose each displayed number at most once")
    return result


def select(prompt, count, default, allow_empty=False):
    while True:
        try: return numbers(ask(prompt, default), count, allow_empty)
        except RuntimeError as error: print(error)


def choose_cards(cards):
    eligible = [c for c in cards if c["supported"]]
    print("\n1. Choose cards\nNumber  PCI address      Link width  Driver")
    for index, card in enumerate(eligible, 1):
        print(f"{index:>6}  {card['bdf']}  x{card['width']:<9} {card.get('driver') or 'none'}")
    for card in cards:
        if not card["supported"]: print("Unsupported board, excluded:", card["bdf"], card["ids"])
    kit.require(eligible, "No supported CMP70HX cards found. Check discover output and the compatibility table.")
    print("The PCI address identifies a card. x4/x1 is its connected lane count, not a card number.")
    print("Choose by the physical slot. Width alone cannot identify your render card.")
    chosen = [eligible[i] for i in select("Enable compute/Tensor on card numbers (e.g. 1,2 or all)", len(eligible), "all")]
    print("\nSelected cards (numbers below apply to this list):")
    for index, card in enumerate(chosen, 1): print(f"  {index}: {card['bdf']} (x{card['width']})")
    gfx = select("Enable graphics unlock on numbers, all, or none", len(chosen), "all", True)
    print("PCIe Gen2 is optional. Select only links you intend to upgrade; normally leave x1 risers unchanged.")
    while True:
        pcie = select("Enable PCIe Gen2 on numbers, or none", len(chosen), "none", True)
        if all(chosen[i]["width"] in (1, 2, 4) for i in pcie): break
        print("Gen2 supports recorded widths x1/x2/x4 only. Choose a different set.")
    return chosen, [chosen[i]["bdf"] for i in gfx], [chosen[i]["bdf"] for i in pcie]


def check_host():
    kit.require(sys.platform == "linux" and kit.platform.machine() == "x86_64", "Run on the Linux x86_64 computer containing the cards")
    kit.require(os.geteuid() != 0, "Run without sudo. Build uses your account; installation requests sudo separately.")
    backend = boot_setup.detect()
    boot_setup.content(backend, sys.executable, 1)
    print("Automatic startup:", backend)
    for tool in ("sudo", "modinfo", "modprobe", "insmod", "make", "cc", "fuser", "nvidia-smi"):
        kit.require(shutil.which(tool), "Missing prerequisite: " + tool + ". Install it with your distribution's package manager and retry.")
    kit.require(kit.output("modinfo", "-F", "version", "nvidia") == kit.VERSION,
                "Install NVIDIA OPEN " + kit.VERSION + " first; the wizard does not install the vendor driver.")
    kit.require("Dual MIT/GPL" in kit.output("modinfo", "-F", "license", "nvidia"), "The OPEN NVIDIA kernel driver is required")
    kit.require(Path("/lib/modules", kit.platform.release(), "build", "Makefile").is_file(),
                "Headers for the running kernel are missing. Install the matching kernel development package and retry.")
    secure = list(Path("/sys/firmware/efi/efivars").glob("SecureBoot-*"))
    if Path("/sys/firmware/efi").exists():
        kit.require(len(secure) == 1 and len(secure[0].read_bytes()) >= 5 and secure[0].read_bytes()[4] == 0,
                    "Secure Boot is enabled or its state is unknown. Use docs/MANUAL.md for build/sign/seal/install.")


def copy_kit(destination):
    import package
    destination.mkdir()
    for source in package.source_files():
        target = destination / source.relative_to(package.ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def get_archive(folder):
    print("\n2. NVIDIA source archive")
    name = ask("Path to the source .tar.xz (Enter to download the official archive)")
    if name:
        path = Path(name).expanduser().resolve()
        kit.require(path.is_file(), "Archive not found: " + str(path))
    else:
        print(SOURCE_URL)
        kit.require(yes("Download this source archive now?"), "Download cancelled; no system files changed")
        path = folder / SOURCE_NAME
        temporary = path.with_suffix(path.suffix + ".part")
        with urllib.request.urlopen(SOURCE_URL, timeout=60) as response, temporary.open("wb") as target:
            shutil.copyfileobj(response, target)
        kit.require(kit.sha(temporary) == kit.ARCHIVE_SHA, "Source archive hash mismatch; partial file retained for inspection")
        temporary.rename(path)
    kit.require(kit.sha(path) == kit.ARCHIVE_SHA, "Wrong source archive checksum. Use the exact official version in README.")
    return path


def prepare():
    kit.require(not kit.PREFIX.exists() and not kit.CONFIG.exists(),
                "A kit installation already exists. Use wizard --resume with its setup directory, or see Recovery.")
    chosen, gfx, pcie = choose_cards(kit.inventory())
    folder = Path.home() / ".local/share/cmp70hx/setups" / str(time.time_ns())
    folder.mkdir(mode=0o700, parents=True)
    copy_kit(folder / "kit")
    kit.configure(argparse.Namespace(cards=",".join(c["bdf"] for c in chosen),
        gfx=",".join(gfx), pcie=",".join(pcie), services="", output=str(folder / "host.json")))
    archive = get_archive(folder)
    print("\n3. Build. Compiler output is shown live and saved in", folder / "build.log")
    print("No GPU settings are changed. After building: choose MME, confirm installation, then restart.")
    command = [sys.executable, "-u", str(folder / "kit/cmp70hx.py"), "build", "--config", str(folder / "host.json"),
               "--archive", str(archive), "--output", str(folder / "build"), "--jobs", "2"]
    stream_build(command, folder / "build.log")
    kit.write_json(folder / "wizard.json", dict(schema=1, phase="built", config_sha256=kit.sha(folder / "host.json"),
                   module_sha256=kit.sha(folder / "build/nvidia.ko")))
    return folder


def stream_build(command, log_path, heartbeat=10, timeout=1900):
    """Tee compiler output live; report elapsed time even when the compiler is quiet."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace", start_new_session=True)
    lines = queue.Queue()
    def reader():
        try:
            for line in process.stdout: lines.put(line)
        finally: lines.put(None)
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    started = last_message = time.monotonic()
    try:
        with Path(log_path).open("w", encoding="utf-8", buffering=1) as log:
            while True:
                now = time.monotonic()
                if now - started > timeout: raise RuntimeError("Build time limit exceeded")
                try: line = lines.get(timeout=min(1, heartbeat))
                except queue.Empty:
                    if now - last_message >= heartbeat:
                        print(f"[Build running: {int(now - started)} seconds elapsed; waiting for compiler output]", flush=True)
                        last_message = now
                    continue
                if line is None: break
                log.write(line)
                print(line, end="", flush=True)
                last_message = time.monotonic()
        code = process.wait(timeout=10)
        kit.require(code == 0, "Build failed. Read " + str(log_path) + "; no installation performed.")
        print(f"Build finished in {int(time.monotonic() - started)} seconds.", flush=True)
    except BaseException:
        # Only this build's process group; never a system/GPU process group.
        try: os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise
    finally:
        thread.join(timeout=2)
        if not thread.is_alive(): process.stdout.close()


def upgrade_setup(folder):
    """Keep a completed build while updating only compatible installer sources."""
    import package
    old = folder / "kit"
    kit.require((old / "PROVENANCE.json").read_bytes() == (package.ROOT / "PROVENANCE.json").read_bytes(),
                "Source overlay changed; create a new setup instead of reusing this build")
    if (old / "cmp70hx.py").read_bytes() == (package.ROOT / "cmp70hx.py").read_bytes() and \
       (old / "cmp70hx_wizard.py").read_bytes() == (package.ROOT / "cmp70hx_wizard.py").read_bytes():
        return
    staged = folder / ("kit-new-" + str(time.time_ns()))
    copy_kit(staged)
    archived = folder / ("kit-previous-" + str(time.time_ns()))
    old.rename(archived)
    try: staged.rename(old)
    except BaseException:
        archived.rename(old)
        raise
    print("Installer updated; existing build retained. Previous sources saved in", archived)


# The privileged interpreter starts from this in-memory bootstrap, never a path
# inside the user's setup. The digest binds the transferred bytes to this invocation.
ROOT_BOOTSTRAP = '''import base64, hashlib, json, pathlib, sys, tempfile
payload = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
if len(payload) > 4 * 1024 * 1024 or hashlib.sha256(payload).hexdigest() != sys.argv[1]:
    raise SystemExit("Installer transfer hash mismatch")
files = json.loads(payload)
if set(files) != {"cmp70hx.py", "cmp70hx_boot.py", "cmp_mme_manage.py"}:
    raise SystemExit("Unexpected installer files")
with tempfile.TemporaryDirectory(prefix="cmp70hx-root-", dir="/tmp") as temporary:
    root = pathlib.Path(temporary)
    for name, encoded in files.items():
        path = root / name
        path.write_bytes(base64.b64decode(encoded, validate=True))
        path.chmod(0o600)
    sys.path.insert(0, str(root))
    import cmp70hx
    cmp70hx.main(sys.argv[2:])
'''


def root_run(*args, timeout=180, input=None):
    result = subprocess.run(["sudo", *[str(a) for a in args]], timeout=timeout, input=input)
    kit.require(result.returncode == 0, "Command failed: " + shlex.join([str(a) for a in args]))


def privileged_prepare(build, libdir):
    import base64
    import hashlib
    files = {name: base64.b64encode(kit.read_regular_file(kit.ROOT / name)).decode("ascii")
             for name in ("cmp70hx.py", "cmp70hx_boot.py", "cmp_mme_manage.py")}
    payload = json.dumps(files, sort_keys=True).encode()
    command = [kit.persistent_python(), "-I", "-c", ROOT_BOOTSTRAP, hashlib.sha256(payload).hexdigest(),
               "prepare-reboot", "--build", build, "--ack-shared-driver"]
    if libdir != "skip": command += ["--lib-dir", str(Path(libdir).expanduser())]
    root_run(*command, input=payload)


def finish(folder):
    folder = folder.expanduser().resolve()
    state = json.loads((folder / "wizard.json").read_text())
    kit.require(state.get("schema") == 1, "Unknown wizard setup format")
    config = kit.load(folder / "build/config.json")
    kit.require(not config["services"], "Wizard setup must be independent of application services")
    kit.require(kit.sha(folder / "build/config.json") == state["config_sha256"] and
                kit.sha(folder / "build/nvidia.ko") == state["module_sha256"], "Prepared configuration/module changed; create a new setup")
    kit.require(config["kernel"] == kit.platform.release(), "Kernel changed since build. Create a new setup.")
    kit.check_hardware(config)
    resume = shlex.join(["python3", str(folder / "kit/cmp70hx.py"), "wizard", "--resume", str(folder)])
    print("\n4. Review installation")
    for card in config["cards"]:
        print(card["bdf"], "compute/Tensor: yes; GFX:", "yes" if card["gfx"] else "no",
              "; PCIe:", "Gen2 x" + str(card["width"]) if card["pcie"] else "unchanged")
    print("Setup and build log:", folder)
    print("Resume command:", resume)
    (folder / "RESUME.txt").write_text(resume + "\n", encoding="utf-8")
    print("The installer prepares files while your applications stay open. GPU activation happens on the next boot.")
    print("Automatic startup:", boot_setup.detect())
    print("All NVIDIA cards are affected by shared driver initialization. Power/voltage/clock profiles stay unchanged.")
    if kit.DISABLED.exists(): print("The previous disable flag will be removed after boot setup succeeds.")
    print("\nMME graphics-library unlock (configured now; applied during boot)")
    candidates = library_directories()
    print("Detected directories:", ", ".join(str(p) for p in candidates) or "none; enter the actual vendor-library directory")
    libdir = ask("Library directory, or skip to leave MME unchanged", str(candidates[0]) if len(candidates) == 1 else "skip")
    print("\n5. Install and restart")
    print("Remaining automatic steps: install files, enable startup, then activate and verify during the next boot.")
    print("Save documents before restarting. A normal system restart closes your sessions and applications, including remote desktop.")
    print("Type REBOOT to prepare and restart now; INSTALL to prepare and restart later; Enter to cancel.")
    action = ask("Your choice").upper()
    if action not in ("REBOOT", "INSTALL"): return
    root_run("-v")
    def save(phase):
        state["phase"] = phase
        kit.write_json(folder / "wizard.json", state)
    try:
        save("preparing-reboot")
        privileged_prepare(folder / "build", libdir)
        state["mme"] = "pending" if libdir != "skip" else "skipped"
        save("awaiting-reboot")
        print("\nFiles installed; activation and verification are pending the next boot.")
        print("After login, check: sudo python3 /usr/local/lib/cmp70hx/cmp70hx.py status --verify")
        print("Boot result: /var/lib/cmp70hx/boot-result.json; full log: /var/lib/cmp70hx/boot.log")
        if action == "REBOOT":
            print("Restart requested. Activation can add a few minutes to the next boot.", flush=True)
            try:
                root_run("reboot")
            except (RuntimeError, OSError, subprocess.SubprocessError) as error:
                print("Automatic restart was not completed:", error)
                print("Installation is prepared. Save your work and restart through the desktop menu.")
                print("You do not need to reinstall or resume setup. Activation will run on the next boot.")
        else:
            print("Restart normally when ready. No application has been closed and no GPU settings have been changed.")
    except BaseException:
        print("\nStopped at:", state["phase"], "\nSaved setup:", folder)
        print("After addressing the reported cause, resume with:", resume)
        print("For recovery see", folder / "kit/docs/RECOVERY.md")
        print("Activation report: /var/lib/cmp70hx/last-run.json")
        raise


def library_directories():
    candidates = {Path(p) for p in ("/usr/lib/x86_64-linux-gnu", "/usr/lib64", "/usr/lib", "/usr/local/lib")}
    if shutil.which("ldconfig"):
        result = subprocess.run(["ldconfig", "-p"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "libnvidia-eglcore.so" in line and " => " in line:
                    candidates.add(Path(line.split(" => ", 1)[1].strip()).resolve().parent)
    return sorted({p.resolve() for p in candidates if all((p / f"libnvidia-{name}.so.{kit.VERSION}").is_file()
                    and not (p / f"libnvidia-{name}.so.{kit.VERSION}").is_symlink() for name in ("eglcore", "glcore"))})


def main(args):
    kit.require(sys.stdin.isatty(), "The wizard needs an interactive terminal. Use --help for scripted commands.")
    print("CMP70HX setup wizard (experimental)\nBuild and install on this Linux computer. Enter accepts the value in brackets.")
    check_host()
    if args.resume:
        folder = Path(args.resume).expanduser().resolve()
        upgrade_setup(folder)
    else:
        folder = prepare()
    finish(folder)
