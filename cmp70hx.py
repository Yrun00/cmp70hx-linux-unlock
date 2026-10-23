#!/usr/bin/env python3
"""CMP70HX Linux source kit. Discovery/build are unprivileged; activation is explicit."""
from __future__ import annotations

import argparse
import contextlib
import difflib
import hashlib
import json
import mmap
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import struct
import stat
import subprocess
import sys
import tarfile
import time
import tempfile

VERSION = "610.43.03"
ARCHIVE_SHA = "7e118923c7a23edc36114d63273a46e3e04e9af98695a42203e7ac2dfe9fc1dc"
ROOT = Path(__file__).resolve().parent
PREFIX = Path("/usr/local/lib/cmp70hx")
CONFIG = Path("/etc/cmp70hx.json")
UNIT = "cmp70hx-unlock.service"
MME_UNIT = "cmp70hx-mme.service"
STATE = Path("/var/lib/cmp70hx")
READY = Path("/run/cmp70hx/ready.json")
DISABLED = Path("/etc/cmp70hx.disabled")
SYSFS = Path("/sys/bus/pci/devices")
SYSTEMD = Path("/etc/systemd/system")
BDF = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-1][0-9a-f]\.0")
SERVICE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_@.:-]*\.service")
IDS = dict(vendor="0x10de", device="0x248a",
           subsystem_vendor="0x10de", subsystem_device="0x248a")
MODULES = ["nvidia_drm", "nvidia_modeset", "nvidia_uvm", "nvidia_peermem", "nvidia"]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid(),
            "Private state directory is symlinked or owned by another user: " + str(path))
    path.chmod(0o700)


@contextlib.contextmanager
def private_log(path):
    private_directory(STATE)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid(), "Unsafe log file")
        os.fchmod(fd, 0o600)
        handle = os.fdopen(fd, "a", buffering=1)
    except BaseException:
        os.close(fd)
        raise
    with handle:
        yield handle


def atomic_write(path, data):
    path = Path(path)
    private = path == CONFIG or path == READY or STATE in path.parents
    if STATE in path.parents:
        private_directory(STATE)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            if os.name == "posix":
                os.fchmod(handle.fileno(), 0o600 if private else 0o644)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    atomic_write(path, (json.dumps(value, indent=2) + "\n").encode())


@contextlib.contextmanager
def admin_lock():
    root_required()
    import fcntl
    private_directory(STATE)
    with (STATE / "admin.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def run(*args, check=True, timeout=120):
    result = subprocess.run([str(a) for a in args], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=timeout)
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if check:
        require(result.returncode == 0, f"Command failed ({result.returncode}): {args[0]}")
    return result


def output(*args):
    return run(*args).stdout.strip()


def run_live(*args, timeout=1800):
    """Let build output reach the terminal/wizard immediately."""
    result = subprocess.run([str(a) for a in args], stderr=subprocess.STDOUT, timeout=timeout)
    require(result.returncode == 0, f"Command failed ({result.returncode}): {args[0]}")
    return result


def persistent_python():
    require(re.fullmatch(r"/(usr/|bin/)[A-Za-z0-9_./+-]+", sys.executable),
            "Boot setup needs a persistent system Python under /usr or /bin; rerun with system Python 3.12+")
    return sys.executable


def nvidia_device_nodes(dev=Path("/dev"), drm=Path("/sys/class/drm")):
    """All NVIDIA client nodes, including DRM nodes; exclude other vendors."""
    devices = set()
    for pattern in ("nvidia[0-9]*", "nvidiactl", "nvidia-uvm*", "nvidia-modeset", "nvidia-caps/*"):
        devices.update(p for p in dev.glob(pattern) if p.exists() and not p.is_dir())
    for node in drm.iterdir() if drm.exists() else []:
        if not re.fullmatch(r"(?:card|renderD)[0-9]+", node.name):
            continue
        vendor = node / "device/vendor"
        if vendor.is_file() and vendor.read_text().strip().lower() == "0x10de":
            path = dev / "dri" / node.name
            if path.exists(): devices.add(path)
    return sorted(devices)


def persistence_snapshot():
    text = run("nvidia-smi", "--query-gpu=uuid,persistence_mode", "--format=csv,noheader", timeout=20).stdout
    result = []
    for line in text.splitlines():
        uuid, mode = [x.strip() for x in line.split(",", 1)]
        require(re.fullmatch(r"GPU-[0-9a-fA-F-]{36}", uuid) and mode in ("Enabled", "Disabled"),
                "Unrecognized persistence inventory")
        require(uuid not in dict(result), "Duplicate GPU in persistence inventory")
        result.append((uuid, mode))
    require(result, "Empty persistence inventory")
    return result


def restore_persistence(previous, report):
    """Restore only after a responsive driver reports the same physical UUIDs."""
    report.update(status="checking", restored=False)
    try:
        current = dict(persistence_snapshot())
        require(set(current) == set(dict(previous)), "GPU inventory changed; persistence restore withheld")
        for uuid, mode in previous:
            if current[uuid] != mode:
                run("nvidia-smi", "-i", uuid, "-pm", "1" if mode == "Enabled" else "0", timeout=20)
        observed = dict(persistence_snapshot())
        report["observed"] = observed
        require(observed == dict(previous), "Persistence restore readback differs")
        report.update(status="restored", restored=True)
    except Exception as error:
        report.update(status="failed", restored=False, error=str(error))
        raise


def inventory(sysfs=SYSFS):
    """Only sysfs PCI metadata; never initializes NVIDIA or maps BARs."""
    cards = []
    for directory in sorted(sysfs.glob("*")):
        try:
            ids = {key: (directory / key).read_text().strip() for key in IDS}
            if ids["vendor"] != IDS["vendor"] or ids["device"] != IDS["device"]:
                continue
            width = int((directory / "current_link_width").read_text().strip())
            driver = (directory / "driver").resolve().name if (directory / "driver").exists() else None
            cards.append(dict(bdf=directory.name, ids=ids, width=width,
                              driver=driver, supported=ids == IDS and bool(BDF.fullmatch(directory.name))))
        except (OSError, ValueError):
            continue
    return cards


def validate(config):
    require(config.get("schema") == 1 and config.get("driver") == VERSION, "Unsupported config/driver")
    require(re.fullmatch(r"[A-Za-z0-9_.+-]+", config.get("kernel", "")), "Invalid kernel")
    cards = config.get("cards", [])
    require(1 <= len(cards) <= 8, "Select 1..8 CMP cards (protected path has eight GPU slots)")
    seen = set()
    for card in cards:
        bdf = card.get("bdf", "")
        require(bool(BDF.fullmatch(bdf)) and bdf not in seen, "Invalid/duplicate BDF; function 0 only")
        seen.add(bdf)
        require(card.get("ids") == IDS, "Only tested 10de:248a / 10de:248a boards are accepted")
        require(card.get("compute") is True, "All selected cards require compute unlock")
        require(type(card.get("pcie")) is bool and type(card.get("gfx")) is bool, "Boolean policy required")
        require(card.get("width") in (1, 2, 4, 8, 16), "Invalid recorded link width")
        if card["pcie"]:
            require(card["width"] in (1, 2, 4), "Gen2 policy supports recorded widths 1/2/4 only")
    services = config.get("services", [])
    require(len(set(services)) == len(services), "Duplicate service")
    for service in services:
        require(SERVICE.fullmatch(service) and service not in (UNIT, MME_UNIT),
                "Invalid consumer service")
        require(service not in ("ssh.service", "sshd.service", "systemd-logind.service"),
                "Recovery/session infrastructure cannot be a managed consumer")
    stock = config.get("stock_module", "")
    require(stock.startswith("/lib/modules/") or stock.startswith("/usr/lib/modules/"),
            "Stock module must remain in the distro module tree")
    require(not any(c.isspace() for c in stock) and ".." not in Path(stock).parts, "Invalid stock path")
    return config


def load(path):
    return validate(json.loads(Path(path).read_text()))


def selected(config, feature):
    return [c for c in config["cards"] if c[feature]]


def conditions(cards, rm=False):
    terms = []
    for card in cards:
        domain, bus, slot = card["bdf"].split(":")
        device = slot.split(".")[0]
        if rm:
            terms.append(f"(gpuGetDomain(pGpu) == 0x{domain} && gpuGetBus(pGpu) == 0x{bus} && gpuGetDevice(pGpu) == 0x{device})")
        else:
            terms.append(f"(pci_domain_nr(p->bus) == 0x{domain} && p->bus->number == 0x{bus} && p->devfn == 0x{int(device,16)*8:02x})")
    return "(" + " || ".join(terms) + ")" if terms else "0"


def replace_once(text, old, new):
    require(text.count(old) == 1, "Source template mismatch: " + old[:70])
    return text.replace(old, new)


def replace_function(text, signature, replacement):
    start = text.index(signature)
    opening = text.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        if text[end] == "{":
            depth += 1
        elif text[end] == "}":
            depth -= 1
        end += 1
    return text[:start] + replacement + text[end:]


def generate_sources(config, destination, root=ROOT):
    validate(config)
    provenance = json.loads((root / "PROVENANCE.json").read_text())
    for entry in provenance["files"]:
        require(sha(root / entry["file"]) == entry["sha256"], "Overlay hash mismatch")
        relative = Path(entry["file"]).relative_to("overlay")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / entry["file"], target)
    all_c = conditions(config["cards"])
    pcie_c = conditions(selected(config, "pcie"))
    gfx_c = conditions(selected(config, "gfx"))
    nv_path = destination / "kernel-open/nvidia/nv.c"
    nv = nv_path.read_text()
    nv = replace_once(nv, "#error CMP70HX_TEMPLATE_REQUIRES_GENERATION\n", "")
    target = f"""static NvBool nv_cmp70_target(nv_state_t *nv)
{{
    struct pci_dev *p = NV_GET_NVL_FROM_NV_STATE(nv)->pci_dev;
    if (!p || p->vendor != 0x10de || p->device != 0x248a ||
        p->subsystem_vendor != 0x10de || p->subsystem_device != 0x248a) return NV_FALSE;
    if (cmp70_port_mode == 4)
        return cmp70_port_write_index == 3 ? {gfx_c} : {pcie_c};
    return {all_c};
}}
static NvBool nv_cmp70_pcie_target(struct pci_dev *p) {{ return p && {pcie_c}; }}
static NvBool nv_cmp70_gfx_target(struct pci_dev *p) {{ return p && {gfx_c}; }}
static unsigned int nv_cmp70_expected_width(struct pci_dev *p)
{{
"""
    for card in selected(config, "pcie"):
        target += f"    if ({conditions([card])}) return {card['width']};\n"
    target += "    return 0;\n}"
    nv = replace_function(nv, "static NvBool nv_cmp70_target(", target)
    nv = replace_once(nv, "if (cmp70_port_mode != 5 || !nv_cmp70_target(nv)) return;",
                      "if (cmp70_port_mode != 5 || !nv_cmp70_target(nv) || !nv_cmp70_pcie_target(gpu)) return;")
    nv = replace_once(nv, "PCI_EXP_LNKSTA_NLW_SHIFT) == 4)",
                      "PCI_EXP_LNKSTA_NLW_SHIFT) == nv_cmp70_expected_width(gpu))")
    nv = nv.replace("SUCCESS Gen2 x4", "SUCCESS Gen2 configured-width").replace("no Gen2 x4", "no Gen2 configured-width")
    nv = replace_once(nv, "/* CMP70HX_GFX_TARGET */ !p", "!nv_cmp70_gfx_target(p)")
    nv_path.write_text(nv, encoding="utf-8", newline="\n")
    header = destination / "src/nvidia/inc/kernel/gpu/gsp/cmp70_port.h"
    text = header.read_text()
    rm = f"""static NV_INLINE NvBool cmp70PortAddress(OBJGPU *pGpu)
{{
    NvU32 id = pGpu->idInfo.PCIDeviceID;
    if (!gpuIsDBDFValid(pGpu) ||
        !(id == 0x248a10deU || id == 0x248aU || (id >> 16) == 0x248aU) ||
        pGpu->idInfo.PCISubDeviceID != 0x248a10deU) return NV_FALSE;
    if (osCmp70PortMode() == 4)
        return osCmp70PortWriteIndex() == 3 ?
            {conditions(selected(config, 'gfx'), True)} :
            {conditions(selected(config, 'pcie'), True)};
    return {conditions(config['cards'], True)};
}}"""
    text = replace_function(text, "static NV_INLINE NvBool cmp70PortAddress(", rm)
    text = replace_once(text, "#error CMP70HX_TEMPLATE_REQUIRES_GENERATION\n", "")
    header.write_text(text, encoding="utf-8", newline="\n")


def unpack(archive, destination):
    require(sha(archive) == ARCHIVE_SHA, "Official 610.43.03 source archive SHA256 mismatch")
    with tarfile.open(archive, "r:xz") as source:
        # Python's data filter rejects traversal, links outside the destination and device files.
        source.extractall(destination, filter="data")
    children = list(destination.iterdir())
    require(len(children) == 1 and children[0].is_dir(), "Unexpected archive layout")
    return children[0]


def configure(args):
    available = {c["bdf"]: c for c in inventory()}
    names = sorted(bdf for bdf, card in available.items() if card["supported"]) if args.cards == "all" else args.cards.split(",")
    if args.cards == "all":
        excluded = sorted(set(available) - set(names))
        if excluded:
            print("Excluded unsupported devices: " + ", ".join(excluded))
    require(bool(names), "No supported CMP70HX devices selected")
    require(len(set(names)) == len(names), "Duplicate device")
    pcie = set(filter(None, args.pcie.split(",")))
    gfx = set(names) if args.gfx == "all" else set(filter(None, args.gfx.split(",")))
    require(pcie <= set(names) and gfx <= set(names), "PCIe/GFX targets must be selected compute cards")
    cards = []
    for name in names:
        require(name in available and available[name]["supported"], "Unsupported/not-present device: " + name)
        card = {k: available[name][k] for k in ("bdf", "ids", "width")}
        card.update(compute=True, pcie=name in pcie, gfx=name in gfx)
        cards.append(card)
    require(not Path(args.output).exists(), "Config output exists; choose a new path")
    config = dict(schema=1, driver=VERSION, kernel=platform.release(),
                  stock_module=output("modinfo", "-n", "nvidia"),
                  services=args.services.split(",") if args.services else [], cards=cards)
    write_json(args.output, validate(config))
    print("Review config before building. No power/clock changes are generated.")


def build(args):
    config = load(args.config)
    require(os.name == "posix" and platform.machine() == "x86_64", "Build on Linux x86_64")
    require(platform.release() == config["kernel"], "Build for the currently running configured kernel")
    require(not Path(args.output).exists(), "Build output exists")
    out = Path(args.output).resolve()
    out.mkdir(parents=True)
    source = unpack(args.archive, out / "vendor")
    entries = json.loads((ROOT / "PROVENANCE.json").read_text())["files"]
    relative_sources = [Path(e["file"]).relative_to("overlay") for e in entries]
    originals = {str(p): (source / p).read_text() if (source / p).exists() else "" for p in relative_sources}
    generate_sources(config, source)
    source_diff = "".join("".join(difflib.unified_diff(
        originals[str(p)].splitlines(keepends=True), (source / p).read_text().splitlines(keepends=True),
        fromfile="a/" + p.as_posix(), tofile="b/" + p.as_posix())) for p in relative_sources)
    (out / "host.patch").write_text(source_diff, encoding="utf-8", newline="\n")
    run_live("make", "-C", source, f"-j{args.jobs}", "modules", f"KERNEL_UNAME={config['kernel']}", timeout=1800)
    module = source / "kernel-open/nvidia.ko"
    require(output("modinfo", "-F", "version", module) == VERSION, "Wrong built version")
    require(output("modinfo", "-F", "vermagic", module).split()[0] == config["kernel"], "Wrong vermagic")
    shutil.copyfile(module, out / "nvidia.ko")
    write_json(out / "config.json", config)
    write_json(out / "build.json", dict(module_sha256=sha(out / "nvidia.ko"),
                                      config_sha256=sha(out / "config.json"),
                                      source_archive_sha256=ARCHIVE_SHA, hardware_tested=False,
                                      host_patch_sha256=sha(out / "host.patch"),
                                      generated_sources={str(p.relative_to(source)): sha(p)
                                          for p in [source / Path(e["file"]).relative_to("overlay")
                                                    for e in json.loads((ROOT / "PROVENANCE.json").read_text())["files"]]}))
    print("Build ready:", out)


def seal(args):
    """Record a locally signed module after validating its version and kernel."""
    folder = Path(args.build)
    config = load(folder / "config.json")
    info = json.loads((folder / "build.json").read_text())
    require(sha(folder / "config.json") == info["config_sha256"], "Config changed; rebuild")
    module = folder / "nvidia.ko"
    require(output("modinfo", "-F", "version", module) == VERSION, "Wrong signed module version")
    require(output("modinfo", "-F", "vermagic", module).split()[0] == config["kernel"], "Wrong signed module kernel")
    signer = output("modinfo", "-F", "signer", module)
    require(bool(signer), "Module has no signer; seal is for the local signing step")
    info.update(unsigned_module_sha256=info["module_sha256"], module_sha256=sha(module), signer=signer)
    write_json(folder / "build.json", info)
    print("Recorded signed-module hash. Kernel trust must still be configured on this host.")


def root_required():
    require(os.name == "posix" and os.geteuid() == 0, "This action requires root on Linux")
    os.environ["PATH"] = "/usr/sbin:/usr/bin:/sbin:/bin"
    require(platform.machine() == "x86_64", "Only x86_64 Linux supported")


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def check_hardware(config):
    present = {c["bdf"]: c for c in inventory()}
    for card in config["cards"]:
        require(card["bdf"] in present and present[card["bdf"]]["ids"] == card["ids"],
                "PCI identity changed; reconfigure before applying")
        require(present[card["bdf"]]["width"] == card["width"], "Link width changed; review topology")
    # RM upstream storage has eight slots, including unselected NVIDIA devices.
    nvidia = [p for p in SYSFS.glob("*") if (p / "vendor").is_file()
              and (p / "vendor").read_text().strip() == "0x10de"
              and (p / "class").is_file() and (p / "class").read_text().strip().startswith("0x03")]
    require(len(nvidia) <= 8, "More than eight NVIDIA display-class devices")


def read_registers(card):
    with (SYSFS / card["bdf"] / "resource0").open("rb", buffering=0) as handle:
        with mmap.mmap(handle.fileno(), 0x1000000, access=mmap.ACCESS_READ) as memory:
            read = lambda address: struct.unpack_from("<I", memory, address)[0]
            require(read(0x88000) == 0x248a10de and read(0) not in (0, 0xffffffff), "BAR not live")
            return {address: read(address) for address in
                    (0x88fe8, 0x823800, 0x823804, 0x82381c, 0x823820, 0x823b04, 0x823830, 0x823834)}


def module_loaded(name="nvidia"):
    return Path("/sys/module", name).exists()


def loaded_source():
    return Path("/sys/module/nvidia/srcversion").read_text().strip()


def loaded_policy(config):
    parameters = Path("/sys/module/nvidia/parameters")
    require((parameters / "cmp70_port_mode").read_text().strip() == "5", "Final driver mode is not active")
    expected = "1" if selected(config, "gfx") else "0"
    require((parameters / "cmp70_gfx_test").read_text().strip() == expected, "Loaded GFX policy differs")


def verify(config, baseline=None):
    values = {}
    for card in config["cards"]:
        r = read_registers(card)
        require((r[0x823804], r[0x82381c], r[0x823820]) == (0xffffffff, 0x88888888, 8), "Compute readback failed")
        if card["gfx"]:
            require(r[0x823b04] == 0xffffffff and r[0x823830] & 7 == 4 and r[0x823834] & 3 == 0, "GFX readback failed")
        elif baseline:
            before = baseline[card["bdf"]]
            require((r[0x823830], r[0x823834]) == (before[0x823830], before[0x823834]),
                    "Unselected GFX state changed")
        if card["pcie"]:
            require(r[0x88fe8] == r[0x823800] == 0xffffffff, "PCIe gate closed")
            speed = (SYSFS / card["bdf"] / "current_link_speed").read_text().strip()
            width = int((SYSFS / card["bdf"] / "current_link_width").read_text())
            require(speed.startswith("5.0 GT/s") and width == card["width"], "Gen2 link readback failed")
        values[card["bdf"]] = {hex(k): hex(v) for k, v in r.items()}
    return values


def unload():
    for module in MODULES:
        if Path("/sys/module", module).exists():
            run("modprobe", "-r", module)
    require(not Path("/sys/module/nvidia").exists(), "NVIDIA module remains loaded")


def initialize(module, mode, index=None, gfx=False):
    params = [f"cmp70_port_mode={mode}"]
    if index is not None:
        params.append(f"cmp70_port_write_index={index}")
    if gfx:
        params.append("cmp70_gfx_test=1")
    run("insmod", module, *params)
    run("nvidia-smi", "--query-gpu=pci.bus_id,uuid", "--format=csv", timeout=100)


def stages(config):
    result = [(1, None, False)]
    for index, feature in ((1, "pcie"), (2, "pcie"), (3, "gfx")):
        if selected(config, feature):
            result.append((4, index, False))
    return result + [(2, None, False), (5, None, bool(selected(config, "gfx")))]


class ActivationTerminated(BaseException):
    """Service shutdown: record uncertainty without starting another GPU operation."""


@contextlib.contextmanager
def termination_guard():
    def terminate(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise ActivationTerminated("Activation interrupted by SIGTERM; GPU state requires recovery")
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def apply(args):
    root_required()
    require(args.ack_shared_driver, "Pass --ack-shared-driver after reviewing all NVIDIA clients")
    require(not (STATE / "pending-install.json").exists(), "Installation incomplete; run recover-install")
    config = load(CONFIG)
    require(not DISABLED.exists(), "Unlock disabled by /etc/cmp70hx.disabled")
    check_hardware(config)
    require(platform.release() == config["kernel"], "Kernel changed; rebuild and reinstall")
    build_info = json.loads((PREFIX / "build.json").read_text())
    module = PREFIX / "nvidia.ko"
    require(sha(CONFIG) == build_info["config_sha256"], "Installed config changed; rebuild")
    require(sha(module) == build_info["module_sha256"], "Module SHA256 mismatch")
    require(output("modinfo", "-n", "nvidia") == config["stock_module"], "Stock fallback path changed")
    require(output("modinfo", "-F", "version", config["stock_module"]) == VERSION, "Stock version changed")
    require("Dual MIT/GPL" in output("modinfo", "-F", "license", config["stock_module"]), "OPEN fallback required")
    require(output("modinfo", "-F", "version", module) == VERSION, "Candidate version mismatch")
    require(output("modinfo", "-F", "vermagic", module).split()[0] == config["kernel"], "Candidate kernel mismatch")
    import fcntl
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if READY.exists():
            receipt = json.loads(READY.read_text())
            if (receipt.get("status") == "success" and receipt.get("boot_id") == boot_id() and receipt.get("module_sha256") == sha(module)
                    and receipt.get("config_sha256") == sha(CONFIG)):
                require(loaded_source()
                        == output("modinfo", "-F", "srcversion", module), "Loaded module differs from ready receipt")
                loaded_policy(config)
                verify(config)
                print("Already applied and verified this boot")
                return
        active = []
        success = False
        cycle_started = False
        persistence = []
        persistence_touched = False
        terminated = False
        READY.unlink(missing_ok=True)
        receipt = dict(boot_id=boot_id(), started=time.time(), config_sha256=sha(CONFIG),
                       module_sha256=sha(module), stages=[], status="started")
        write_json(STATE / "last-run.json", receipt)
        try:
            for service in config["services"]:
                if run("systemctl", "is-active", "--quiet", service, check=False).returncode == 0:
                    active.append(service)
                    run("systemctl", "stop", service)
            # Do not disable persistence via NVML while other consumers are still open.
            if module_loaded():
                live_devices = nvidia_device_nodes()
                if live_devices:
                    probe = run("fuser", *live_devices, check=False)
                    require(probe.returncode == 1, "NVIDIA clients remain; close them before activation")
                persistence = persistence_snapshot()
                receipt["persistence_before"] = persistence
                receipt["persistence_restore"] = dict(status="pending", restored=False)
                write_json(STATE / "last-run.json", receipt)
                persistence_touched = True
                run("nvidia-smi", "-pm", "0")
            for mode, index, gfx in stages(config):
                if mode == 4:
                    feature = "gfx" if index == 3 else "pcie"
                    address = {1: 0x88fe8, 2: 0x823800, 3: 0x823b04}[index]
                    opened = [read_registers(c)[address] == 0xffffffff for c in selected(config, feature)]
                    if all(opened):
                        receipt["stages"].append(dict(mode=mode, index=index, skipped="all gates already open"))
                        continue
                    require(not any(opened), "Mixed open/closed gates: stop and cold-reset before a fresh application")
                cycle_started = True
                unload()
                initialize(module, mode, index, gfx)
                receipt["stages"].append(dict(mode=mode, index=index, completed=time.time()))
                write_json(STATE / "last-run.json", receipt)
                if mode == 1:
                    baseline = {c["bdf"]: read_registers(c) for c in config["cards"]}
            receipt["registers"] = verify(config, baseline)
            loaded_policy(config)
            require(loaded_source()
                    == output("modinfo", "-F", "srcversion", module), "Loaded module source mismatch")
            run("modprobe", "nvidia_drm", "modeset=1")
            if persistence_touched:
                restore_persistence(persistence, receipt["persistence_restore"])
            receipt.update(status="success", finished=time.time())
            write_json(READY, receipt)
            success = True
        except BaseException as error:
            receipt.update(status="failed", error=str(error))
            READY.unlink(missing_ok=True)
            terminated = isinstance(error, ActivationTerminated)
            if terminated:
                receipt["recovery"] = "withheld after termination; GPU state unknown, reboot required"
                if persistence_touched:
                    receipt["persistence_restore"] = dict(status="withheld", restored=False,
                        reason="Activation process terminated; no further NVIDIA calls")
                raise
            recovery_ready = not cycle_started
            if cycle_started:
                try:
                    unload()
                    run("modprobe", "nvidia_drm", "modeset=1")
                    receipt["recovery"] = "stock module loaded; register reset not established"
                    recovery_ready = True
                except BaseException as recovery:
                    receipt["recovery"] = "FAILED: " + str(recovery)
            if persistence_touched:
                if recovery_ready:
                    try:
                        restore_persistence(persistence, receipt["persistence_restore"])
                    except Exception:
                        # Preserve the original activation error; restore details are in the receipt.
                        pass
                else:
                    receipt["persistence_restore"] = dict(status="withheld", restored=False,
                        reason="Stock driver recovery failed; no further NVIDIA calls are safe")
            raise
        finally:
            receipt["stopped_services"] = active
            receipt["persistence_before"] = persistence
            # On failed hardware transition leave consumers stopped for diagnosis.
            if not terminated and (success or not cycle_started):
                restored = {}
                for service in active:
                    result = run("systemctl", "start", "--no-block", service, check=False)
                    restored[service] = dict(start_requested=result.returncode == 0, active_observed=None)
                receipt["services"] = restored
            write_json(STATE / "last-run.json", receipt)


def owned_install_path(path):
    path = Path(path)
    names = {"cmp70hx.py", "cmp70hx_boot.py", "cmp_mme_manage.py", "nvidia.ko", "build.json", "install.json"}
    if path.parent == PREFIX and path.name in names:
        return True
    if path == CONFIG or path in (SYSTEMD / UNIT, SYSTEMD / MME_UNIT):
        return True
    return (path.name == "90-cmp70hx.conf" and path.parent.parent == SYSTEMD
            and path.parent.name.endswith(".service.d")
            and bool(SERVICE.fullmatch(path.parent.name[:-2])))


def ensure_inactive_module():
    require(not Path("/sys/module/nvidia/parameters/cmp70_port_mode").exists(),
            "Modified module still loaded; disable the unlock and reboot before cleanup")


def install_transaction(files):
    journal = STATE / "pending-install.json"
    require(not journal.exists(), "Pending installation: run recover-install")
    for path in files:
        require(owned_install_path(path) and not path.exists() and not path.is_symlink(),
                "Installation target already exists or is not owned: " + str(path))
        require(not path.parent.is_symlink(), "Symlinked installation directory: " + str(path.parent))
    identifier = str(time.time_ns())
    plan = dict(schema=1, id=identifier, files=[dict(path=str(p), sha256=hashlib.sha256(data).hexdigest())
                                               for p, data in files.items()])
    # Persist ownership before creating any live file; this survives SIGKILL/reboot.
    write_json(STATE / "transactions" / identifier / "plan.json", plan)
    write_json(journal, plan)
    try:
        for path, data in files.items():
            atomic_write(path, data)
        if SYSTEMD / UNIT in files:
            run("systemctl", "daemon-reload")
            run("systemd-analyze", "verify", UNIT)
    except BaseException as error:
        try:
            recover_install(None)
        except BaseException as recovery:
            raise RuntimeError(f"Installation failed: {error}; recovery pending: {recovery}. Run recover-install.") from error
        raise
    journal.unlink()


def recover_install(args):
    root_required()
    ensure_inactive_module()
    journal = STATE / "pending-install.json"
    if not journal.exists():
        # Compatibility with interrupted 0.1.0 installers that had no journal.
        required = [CONFIG, PREFIX / "install.json", PREFIX / "nvidia.ko",
                    PREFIX / "cmp70hx.py", PREFIX / "cmp_mme_manage.py", PREFIX / "build.json"]
        require(not all(p.is_file() for p in required), "Complete installation exists; use uninstall")
        import cmp70hx_boot
        cmp70hx_boot.detach_openrc()
        detach_integration()
        warnings = restore_mme_if_configured()
        archive = archive_installation("incomplete")
        write_json(archive / "removal.json", dict(integration_removed=True, warnings=warnings))
        for warning in warnings:
            print("WARNING:", warning)
        print("Incomplete installation removed; retry install")
        return
    plan = json.loads(journal.read_text())
    require(plan.get("schema") == 1 and re.fullmatch(r"[0-9]+", plan.get("id", "")), "Invalid install journal")
    paths = []
    for entry in plan["files"]:
        path = Path(entry["path"])
        require(owned_install_path(path) and not path.is_symlink() and not path.parent.is_symlink(),
                "Invalid journal target")
        if path.exists():
            require(path.is_file() and sha(path) == entry["sha256"],
                    "Recovery preserves changed file; inspect before retry: " + str(path))
            paths.append(path)
    has_units = any(Path(entry["path"]).parent == SYSTEMD for entry in plan["files"])
    if (SYSTEMD / UNIT).exists() and has_units:
        run("systemctl", "disable", "--now", UNIT, check=False)
    archive = STATE / "transactions" / plan["id"]
    for index, path in enumerate(paths):
        atomic_write(archive / f"removed-{index}-{path.name}", path.read_bytes())
        path.unlink()
    if has_units:
        run("systemctl", "daemon-reload")
    if PREFIX.exists():
        if any(PREFIX.iterdir()):
            # A killed atomic write may leave an unfinished temporary file.
            shutil.move(str(PREFIX), str(archive / ("remaining-" + str(time.time_ns()))))
        else:
            PREFIX.rmdir()
    write_json(archive / "recovered.json", dict(status="rolled-back", files=[str(p) for p in paths]))
    journal.unlink()
    print("Rolled back incomplete installation; evidence:", archive)


def read_regular_file(path):
    """Read one opened inode; refuse links, devices and FIFOs before reading."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        require(stat.S_ISREG(os.fstat(fd).st_mode), "Expected a regular input file: " + str(path))
        handle = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with handle:
        return handle.read()


@contextlib.contextmanager
def installation_snapshot(build):
    """Validate and install a private copy; never reread user-owned inputs later."""
    private_directory(STATE)
    sources = {name: Path(build) / name for name in ("config.json", "build.json", "nvidia.ko")}
    sources.update({name: ROOT / name for name in ("cmp70hx.py", "cmp70hx_boot.py", "cmp_mme_manage.py")})
    with tempfile.TemporaryDirectory(prefix="install-snapshot-", dir=STATE) as temporary:
        folder = Path(temporary)
        for name, path in sources.items():
            atomic_write(folder / name, read_regular_file(path))
        info = json.loads((folder / "build.json").read_text())
        require(sha(folder / "nvidia.ko") == info["module_sha256"], "Build module hash mismatch in private snapshot")
        require(sha(folder / "config.json") == info["config_sha256"], "Build config hash mismatch in private snapshot")
        load(folder / "config.json")
        yield folder


def install(args):
    root_required()
    require(args.ack_shared_driver, "Installation requires --ack-shared-driver")
    with installation_snapshot(args.build) as folder:
        install_snapshot(args, folder)


def install_snapshot(args, folder):
    """Internal installer; folder is owned by the privileged snapshot context."""
    if (STATE / "pending-install.json").exists():
        recover_install(args)
    config = load(folder / "config.json")
    standalone = getattr(args, "standalone", False)
    interpreter = sys.executable if standalone else persistent_python()
    require(not standalone or not config["services"], "Standalone installation must have an empty services list")
    require(platform.release() == config["kernel"], "Kernel mismatch")
    check_hardware(config)
    require(not PREFIX.exists() and not CONFIG.exists(),
            "Installation exists: use recover-install for an interrupted install, or uninstall for a complete one")
    for conflict in ("cmp70-unlock.service", "cmp-mme.service", UNIT, MME_UNIT):
        if standalone:
            for directory in (SYSTEMD, Path("/run/systemd/system"), Path("/usr/lib/systemd/system"), Path("/lib/systemd/system")):
                path = directory / conflict
                require(not path.exists() and not path.is_symlink(), "Existing unlock service conflicts: " + str(path))
        else:
            r = run("systemctl", "show", conflict, "-p", "LoadState", "--value", check=False)
            require(r.stdout.strip() in ("not-found", ""), "Existing unlock service conflicts: " + conflict)
    require(output("modinfo", "-n", "nvidia") == config["stock_module"], "Stock fallback mismatch")
    require(output("modinfo", "-F", "version", "nvidia") == VERSION, "Wrong installed driver")
    require("Dual MIT/GPL" in output("modinfo", "-F", "license", "nvidia"), "OPEN modules required")
    for service in config["services"]:
        require(not Path(SYSTEMD, service + ".d/90-cmp70hx.conf").exists(),
                "Consumer drop-in already exists")
        require(output("systemctl", "show", service, "-p", "LoadState", "--value") == "loaded",
                "Consumer service missing or masked: " + service)
    for name in (UNIT, MME_UNIT):
        require(not Path(SYSTEMD, name).is_symlink()
                and not Path(SYSTEMD, name).exists(), "Unit path already exists")
    before = " ".join(sorted(set(config["services"] + ["display-manager.service", "nvidia-persistenced.service"])))
    unit = f"""[Unit]
Description=CMP70HX selected-device unlock
After=local-fs.target systemd-modules-load.service systemd-udev-trigger.service
Before={before}
RequiresMountsFor={PREFIX} {STATE}

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=/usr/bin/udevadm settle --timeout=30
ExecStart={interpreter} {PREFIX}/cmp70hx.py apply --ack-shared-driver
TimeoutStartSec={max(480, 180 * len(config['cards']) + 300)}
TimeoutStopSec=30
UMask=0022

[Install]
WantedBy=multi-user.target
"""
    files = {
        PREFIX / "cmp70hx.py": (folder / "cmp70hx.py").read_bytes(),
        PREFIX / "cmp70hx_boot.py": (folder / "cmp70hx_boot.py").read_bytes(),
        PREFIX / "cmp_mme_manage.py": (folder / "cmp_mme_manage.py").read_bytes(),
        PREFIX / "nvidia.ko": (folder / "nvidia.ko").read_bytes(),
        PREFIX / "build.json": (folder / "build.json").read_bytes(),
        CONFIG: (folder / "config.json").read_bytes(),
    }
    if not standalone:
        files[SYSTEMD / UNIT] = unit.encode()
    dropins = []
    for service in config["services"]:
        path = SYSTEMD / (service + ".d/90-cmp70hx.conf")
        files[path] = f"[Unit]\nRequires={UNIT}\nAfter={UNIT}\n".encode()
        dropins.append(str(path))
    files[PREFIX / "install.json"] = (json.dumps(dict(dropins=dropins, mme=False,
        installation_state="complete", integration="standalone" if standalone else "systemd"), indent=2) + "\n").encode()
    install_transaction(files)
    if standalone and not getattr(args, "preparing_reboot", False):
        print("Installed without boot services. Run the installed controller: apply --ack-shared-driver")
    elif not standalone:
        print("Installed, not enabled or started. Consumer dependencies can request this unit; keep consumers stopped until ready.")


def standalone_installation():
    manifest = PREFIX / "install.json"
    return manifest.is_file() and json.loads(manifest.read_text()).get("integration") == "standalone"


def kernel_ready():
    """Kernel-stage prerequisite, also used before applying MME during boot."""
    if not standalone_installation():
        for key, expected in (("ActiveState", "active"), ("SubState", "exited"), ("Result", "success")):
            require(output("systemctl", "show", UNIT, "-p", key, "--value") == expected,
                    "Unlock service not successfully completed; no GPU probes")
    require(READY.is_file(), "No successful activation receipt for this boot. See /var/lib/cmp70hx/boot-result.json and boot.log")
    receipt = json.loads(READY.read_text())
    require(receipt.get("status") == "success" and receipt["boot_id"] == boot_id()
            and receipt["config_sha256"] == sha(CONFIG), "Missing successful current-boot ready marker")
    require(receipt["module_sha256"] == sha(PREFIX / "nvidia.ko"), "Ready module hash differs")
    require(loaded_source() == output("modinfo", "-F", "srcversion", PREFIX / "nvidia.ko"), "Loaded module differs from ready marker")
    loaded_policy(load(CONFIG))


def verify_mme(data):
    if not data.get("mme"):
        return None
    import cmp_mme_manage
    result = cmp_mme_manage.Manager(lib_dir=data["lib_dir"]).inspect()
    expected = "patched" if data.get("mme_enabled", True) else "original"
    require(len(result["files"]) == 2 and all(f["state"] == expected for f in result["files"]),
            "MME libraries differ from the selected policy; inspect mme status and boot.log")
    return result


def service_ready():
    """Readiness of every configured feature, not just kernel activation."""
    require(not DISABLED.exists(), "Unlock disabled by /etc/cmp70hx.disabled")
    kernel_ready()
    data = json.loads((PREFIX / "install.json").read_text())
    path = STATE / "boot-result.json"
    result = json.loads(path.read_text()) if path.exists() else {}
    if data.get("boot_backend") or data.get("first_boot_pending"):
        require(result.get("status") == "success" and result.get("boot_id") == boot_id(),
                "No completed boot activation for this boot; inspect boot-result.json and boot.log")
        receipt = json.loads(READY.read_text())
        require(result.get("config_sha256") == sha(CONFIG)
                and result.get("module_sha256") == sha(PREFIX / "nvidia.ko")
                and result.get("kernel_started") == receipt.get("started"),
                "Boot result differs from the current activation")
    verify_mme(data)


def status(args):
    print(json.dumps(dict(devices=inventory(), installed=CONFIG.exists(),
                          disabled=DISABLED.exists()), indent=2))
    if (STATE / "boot-result.json").exists():
        result = json.loads((STATE / "boot-result.json").read_text())
        print("Last boot activation:", json.dumps(result, indent=2))
    if CONFIG.exists():
        if not standalone_installation():
            run("systemctl", "show", UNIT, "-p", "ActiveState", "-p", "SubState", "-p", "Result", check=False)
        if (STATE / "last-run.json").exists():
            print((STATE / "last-run.json").read_text())
    if args.verify:
        root_required()
        service_ready()
        config = load(CONFIG)
        info = json.loads((PREFIX / "build.json").read_text())
        require(sha(PREFIX / "nvidia.ko") == info["module_sha256"], "Installed module hash changed")
        require(loaded_source()
                == output("modinfo", "-F", "srcversion", PREFIX / "nvidia.ko"), "Loaded module mismatch")
        loaded_policy(config)
        print(json.dumps(verify(config), indent=2))


def mme(args):
    root_required()
    require(PREFIX.exists(), "Install kit first")
    import cmp_mme_manage as manager
    standalone = standalone_installation()
    if args.action == "install":
        kernel_ready()
        folder = Path(args.lib_dir).resolve()
        require(folder.is_absolute() and re.fullmatch(r"/[A-Za-z0-9_./+-]+", str(folder)),
                "Use an absolute library path without systemd specifiers or shell syntax")
        inspect = manager.Manager(lib_dir=folder).inspect()
        require(all(x["state"] in ("original", "patched") for x in inspect["files"]), "Library hashes unsupported")
        unit_path = Path(SYSTEMD, MME_UNIT)
        before = " ".join(sorted(set(load(CONFIG)["services"] + ["display-manager.service"])))
        interpreter = sys.executable if standalone else persistent_python()
        unit = f"""[Unit]
Description=CMP70HX exact-hash MME patch
After=local-fs.target {UNIT}
Requires={UNIT}
Before={before}

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={interpreter} {PREFIX}/cmp_mme_manage.py apply --lib-dir {folder}
TimeoutStartSec=60

[Install]
WantedBy=multi-user.target
"""
        data = json.loads((PREFIX / "install.json").read_text())
        require(not data.get("mme") or data.get("lib_dir") == str(folder), "Existing MME library directory differs")
        require(not unit_path.is_symlink() and (not unit_path.exists() or unit_path.read_text() == unit),
                "MME unit exists with different content; preserved")
        # Save recovery information before writing/enabling the service. Repeating
        # this command completes the same operation after any interruption.
        data.update(mme=True, mme_enabled=True, lib_dir=str(folder), mme_state="pending")
        write_json(PREFIX / "install.json", data)
        try:
            if standalone:
                run(sys.executable, PREFIX / "cmp_mme_manage.py", "apply", "--lib-dir", folder)
            else:
                atomic_write(unit_path, unit.encode())
                run("systemctl", "daemon-reload")
                run("systemd-analyze", "verify", MME_UNIT)
                run("systemctl", "enable", MME_UNIT)
                run("systemctl", "restart", MME_UNIT)
            data["mme_state"] = "complete"
            verify_mme(data)
            data.pop("mme_error", None)
        except BaseException as error:
            data.update(mme_state="pending", mme_error=str(error))
            write_json(PREFIX / "install.json", data)
            raise
        write_json(PREFIX / "install.json", data)
    else:
        data = json.loads((PREFIX / "install.json").read_text())
        require(data.get("mme"), "No installed MME configuration")
        if args.action == "restore":
            # Persist intent first: interrupted restoration must not re-enable MME at boot.
            data.update(mme_enabled=False, mme_state="restore-pending")
            write_json(PREFIX / "install.json", data)
        if args.action == "restore" and not standalone:
            run("systemctl", "disable", "--now", MME_UNIT)
        run(sys.executable, PREFIX / "cmp_mme_manage.py",
            args.action, "--lib-dir", data["lib_dir"])
        if args.action == "restore":
            verify_mme(data)
            data.update(mme_state="restored")
            data.pop("mme_error", None)
            write_json(PREFIX / "install.json", data)


def detach_integration():
    """Remove only recognized integration; does not depend on a kernel/config file."""
    units = []
    for name in (UNIT, MME_UNIT):
        path = SYSTEMD / name
        if path.exists() or path.is_symlink():
            require(not path.is_symlink() and path.is_file(), "Preserving unrecognized unit: " + str(path))
            content = path.read_text()
            require("Description=CMP70HX" in content and str(PREFIX) + "/" in content,
                    "Preserving changed/unrecognized unit: " + str(path))
            units.append(path)
    expected = f"[Unit]\nRequires={UNIT}\nAfter={UNIT}\n"
    dropins = list(SYSTEMD.glob("*.service.d/90-cmp70hx.conf"))
    for path in dropins:
        require(owned_install_path(path) and not path.is_symlink() and not path.parent.is_symlink()
                and path.read_text() == expected, "Preserving changed consumer drop-in: " + str(path))
    for path in units:
        run("systemctl", "disable", "--now", path.name, check=False)
    if dropins or units:
        backup = STATE / ("detached-services-" + str(time.time_ns()))
        for index, path in enumerate(dropins + units):
            atomic_write(backup / f"{index}-{path.name}", path.read_bytes())
    for path in dropins + units:
        path.unlink()
    if units or dropins:
        run("systemctl", "daemon-reload")
        run("systemctl", "reset-failed", UNIT, MME_UNIT,
            *[p.parent.name[:-2] for p in dropins], check=False)


def archive_installation(label):
    archive = STATE / (label + "-" + str(time.time_ns()))
    archive.mkdir(parents=True)
    if PREFIX.exists():
        require(not PREFIX.is_symlink(), "Refusing to move a symlinked installation")
        shutil.move(str(PREFIX), str(archive / "installation"))
    if CONFIG.exists():
        shutil.move(str(CONFIG), str(archive / "config.json"))
    if READY.exists():
        shutil.move(str(READY), str(archive / "ready.json"))
    return archive


def restore_mme_if_configured():
    warnings = []
    manifest = PREFIX / "install.json"
    try:
        data = json.loads(manifest.read_text()) if manifest.exists() else {}
    except (OSError, ValueError) as error:
        data = {}
        warnings.append("Cannot read MME metadata; inspect preserved backup: " + str(error))
    if data.get("mme"):
        try:
            run(sys.executable, PREFIX / "cmp_mme_manage.py", "restore", "--lib-dir", data["lib_dir"])
        except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            warnings.append("MME restore incomplete; current libraries preserved for review: " + str(error))
    return warnings


def uninstall(args):
    root_required()
    require(DISABLED.exists(), "First create /etc/cmp70hx.disabled and reboot")
    ensure_inactive_module()
    if (STATE / "pending-install.json").exists():
        recover_install(args)
        return
    # Detach first so a changed kernel/driver or failed library restore cannot
    # retain a desktop dependency on an unusable unlock service.
    import cmp70hx_boot
    cmp70hx_boot.detach_openrc()
    detach_integration()
    warnings = restore_mme_if_configured()
    archive = archive_installation("uninstalled")
    write_json(archive / "removal.json", dict(integration_removed=True, warnings=warnings))
    print("Removed service integration; preserved installation/backups/logs:", archive)
    for warning in warnings:
        print("WARNING:", warning)
    print("Verify the current distro driver before restarting consumers. Disable marker retained.")


def boot(args):
    with termination_guard():
        boot_transaction(args)


def boot_failure(args):
    """systemd ExecStopPost: filesystem-only cleanup, including after SIGKILL."""
    result = os.environ.get("SERVICE_RESULT", "unknown")
    path = STATE / "boot-result.json"
    try:
        receipt = json.loads(path.read_text())
    except (OSError, ValueError):
        receipt = {}
    if receipt.get("boot_id") != boot_id():
        receipt = dict(boot_id=boot_id())
    if result == "success" and receipt.get("status") != "running":
        return
    READY.unlink(missing_ok=True)
    atomic_write(DISABLED, b"Boot service failed; inspect boot-result.json and recover before retrying\n")
    receipt.update(status="failed", finished=time.time(), service_result=result,
                   service_exit_code=os.environ.get("EXIT_CODE"), service_exit_status=os.environ.get("EXIT_STATUS"))
    receipt.setdefault("error", "Boot service terminated before completing activation")
    receipt["stop_post"] = "Readiness removed and retries disabled; GPU state not probed or recovered"
    write_json(path, receipt)


def boot_transaction(args):
    require(standalone_installation(), "Independent boot entry requires standalone installation")
    if DISABLED.exists():
        print("CMP70HX startup disabled by", DISABLED)
        return
    result_path = STATE / "boot-result.json"
    receipt = dict(boot_id=boot_id(), started=time.time(), status="running")
    try:
        previous = json.loads(result_path.read_text()) if result_path.exists() else {}
        require(previous.get("status") != "running",
                "Previous boot activation was interrupted. Automatic retry disabled; inspect boot.log before recovery.")
        write_json(result_path, receipt)
        with private_log(STATE / "boot.log") as logfile:
            class Tee:
                def write(self, text):
                    logfile.write(text)
                    return sys.__stdout__.write(text)
                def flush(self):
                    logfile.flush(); sys.__stdout__.flush()
            with contextlib.redirect_stdout(Tee()), contextlib.redirect_stderr(Tee()):
                print("CMP70HX boot activation", receipt["boot_id"], flush=True)
                apply(argparse.Namespace(ack_shared_driver=True))
                data = json.loads((PREFIX / "install.json").read_text())
                if data.get("mme") and data.get("mme_enabled", True):
                    mme(argparse.Namespace(action="install", lib_dir=data["lib_dir"]))
                # mme() updates install.json; do not overwrite it with an old snapshot.
                data = json.loads((PREFIX / "install.json").read_text())
                receipt["mme"] = verify_mme(data)
                kernel = json.loads(READY.read_text())
                require(kernel.get("status") == "success" and kernel.get("boot_id") == receipt["boot_id"],
                        "Kernel activation did not produce a current-boot success receipt")
                receipt.update(config_sha256=sha(CONFIG), module_sha256=sha(PREFIX / "nvidia.ko"),
                               kernel_started=kernel["started"])
                data["first_boot_pending"] = False
                write_json(PREFIX / "install.json", data)
        receipt.update(status="success", finished=time.time())
        write_json(result_path, receipt)
    except BaseException as error:
        READY.unlink(missing_ok=True)
        atomic_write(DISABLED, b"Automatic startup stopped after an activation failure. See /var/lib/cmp70hx/boot-result.json\n")
        receipt.update(status="failed", error=str(error), finished=time.time())
        write_json(result_path, receipt)
        print("Boot activation failed; automatic retry disabled:", error, file=sys.stderr)
        raise


def prepare_reboot(args):
    """Stage files and startup only. NVIDIA clients may stay open until reboot."""
    root_required()
    require(args.ack_shared_driver, "Installation requires --ack-shared-driver")
    with installation_snapshot(args.build) as folder:
        prepare_reboot_snapshot(args, folder)


def prepare_reboot_snapshot(args, folder):
    config = load(folder / "config.json")
    require(not config["services"], "Reboot installation must not manage application services")
    library = None
    if args.lib_dir:
        import cmp_mme_manage
        library = str(Path(args.lib_dir).expanduser().resolve())
        require(re.fullmatch(r"/[A-Za-z0-9_./+-]+", library), "Unsupported library path syntax")
        inspection = cmp_mme_manage.Manager(lib_dir=library).inspect()
        require(len(inspection["files"]) == 2 and all(f["state"] in ("original", "patched") for f in inspection["files"]),
                "MME library hashes unsupported; no installation performed")
    import cmp70hx_boot
    backend = cmp70hx_boot.detect()
    cmp70hx_boot.content(backend, sys.executable, len(config["cards"]))
    if (STATE / "pending-install.json").exists():
        recover_install(args)
    if PREFIX.exists() or CONFIG.exists():
        require(standalone_installation(), "Existing installation has different integration; use its recovery procedure")
        for original, installed in ((folder / "config.json", CONFIG), (folder / "nvidia.ko", PREFIX / "nvidia.ko"),
                                    (folder / "cmp70hx.py", PREFIX / "cmp70hx.py"),
                                    (folder / "cmp70hx_boot.py", PREFIX / "cmp70hx_boot.py"),
                                    (folder / "cmp_mme_manage.py", PREFIX / "cmp_mme_manage.py"),
                                    (folder / "build.json", PREFIX / "build.json")):
            require(installed.is_file() and sha(original) == sha(installed), "Existing installation differs; use its uninstall/recovery procedure")
    else:
        install_snapshot(argparse.Namespace(ack_shared_driver=True, standalone=True, preparing_reboot=True), folder)
    data = json.loads((PREFIX / "install.json").read_text())
    require(not data.get("mme") or library == data.get("lib_dir"), "Existing MME selection differs; preserve its directory or restore it first")
    # Disable before changing the intended boot policy, including interrupted setup.
    atomic_write(DISABLED, b"Reboot installation setup in progress\n")
    data.update(first_boot_pending=True, mme=library is not None, mme_enabled=library is not None)
    if library is not None:
        data.update(lib_dir=library, mme_state="pending")
    write_json(PREFIX / "install.json", data)
    # Interrupted setup stays disabled until startup registration has succeeded.
    cmp70hx_boot.enable()
    write_json(STATE / "setup-result.json", dict(status="awaiting-reboot", configured_boot_id=boot_id(),
        config_sha256=sha(CONFIG), module_sha256=sha(PREFIX / "nvidia.ko"), mme=library, backend=backend))
    DISABLED.unlink()
    print("Prepared successfully. GPU state and running applications are unchanged. Activation will occur on the next boot.")


def autostart(args):
    import cmp70hx_boot
    getattr(cmp70hx_boot, args.action)()


def main(argv=None):
    require(sys.version_info >= (3, 12), "Python 3.12 or newer is required")
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["wizard"]
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("wizard", help="Interactive card selection, build and installation")
    p.add_argument("--resume", help="Continue a prepared wizard setup directory")
    p = sub.add_parser("autostart", help="Independent systemd/OpenRC startup for standalone installations")
    p.add_argument("action", choices=["enable", "disable", "status"])
    sub.add_parser("boot", help="Internal entry point used by the installed boot service")
    sub.add_parser("boot-failure", help="Internal systemd failure receipt handler; no GPU operations")
    p = sub.add_parser("prepare-reboot", help="Install and schedule activation for next boot; leave running applications open")
    p.add_argument("--build", required=True)
    p.add_argument("--lib-dir", help="Stage exact-hash MME activation for this directory at boot")
    p.add_argument("--ack-shared-driver", action="store_true")
    sub.add_parser("discover")
    p = sub.add_parser("configure")
    p.add_argument("--cards", required=True, help="Comma-separated BDFs or all")
    p.add_argument("--pcie", default="", help="Explicit comma-separated Gen2 targets; default none")
    p.add_argument("--gfx", default="all", help="BDFs, all, or empty string for no GFX")
    p.add_argument("--services", default="", help="Explicit consumer .service units to stop/order")
    p.add_argument("--output", default="host.json")
    p = sub.add_parser("build")
    p.add_argument("--config", required=True)
    p.add_argument("--archive", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--jobs", type=int, choices=range(1, 65), default=2)
    p = sub.add_parser("install")
    p.add_argument("--build", required=True)
    p.add_argument("--ack-shared-driver", action="store_true")
    p.add_argument("--standalone", action="store_true", help="Install without systemd, boot hooks or consumer service dependencies")
    p = sub.add_parser("seal")
    p.add_argument("--build", required=True)
    p = sub.add_parser("apply")
    p.add_argument("--ack-shared-driver", action="store_true")
    p = sub.add_parser("status")
    p.add_argument("--verify", "--verify-all", dest="verify", action="store_true",
                   help="Verify current activation, kernel readbacks and all configured MME library hashes")
    p = sub.add_parser("mme")
    p.add_argument("action", choices=["install", "status", "restore"])
    p.add_argument("--lib-dir", default="/usr/lib/x86_64-linux-gnu")
    sub.add_parser("uninstall")
    sub.add_parser("recover-install")
    args = parser.parse_args(argv)
    if args.command == "discover":
        print(json.dumps(inventory(), indent=2))
    elif args.command == "wizard":
        require((ROOT / "cmp70hx_wizard.py").is_file(), "Start the wizard from the downloaded kit or PYZ, not the installed controller")
        import cmp70hx_wizard
        cmp70hx_wizard.main(args)
    elif args.command in ("install", "uninstall", "recover-install", "apply", "mme", "autostart", "boot", "boot-failure", "prepare-reboot") or (args.command == "status" and args.verify):
        with admin_lock():
            globals()[args.command.replace("-", "_")](args)
    else:
        globals()[args.command](args)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled. If installation had begun, inspect the saved setup and service log before resuming.", file=sys.stderr)
        sys.exit(130)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
