"""Independent boot integration for systemd and OpenRC; no application hooks."""
import json
from pathlib import Path
import shutil
import sys
import time

import cmp70hx as kit

OPENRC_SCRIPT = Path("/etc/init.d/cmp70hx-unlock")
OPENRC_RUNLEVEL = Path("/etc/runlevels/boot/cmp70hx-unlock")


def detect():
    if Path("/run/systemd/system").is_dir() and shutil.which("systemctl") and shutil.which("systemd-analyze"):
        return "systemd"
    if Path("/run/openrc").is_dir() and shutil.which("rc-update") and Path("/sbin/openrc-run").is_file():
        return "openrc"
    raise RuntimeError("Automatic startup supports systemd and OpenRC. This init system is not supported yet; use manual installation without boot integration.")


def content(backend, interpreter, cards):
    # Use the same validation as the advanced systemd installation path.
    kit.require(interpreter == kit.persistent_python(), "Boot interpreter differs from the running Python")
    helper = str(kit.PREFIX / "cmp70hx.py")
    if backend == "systemd":
        return f"""[Unit]
Description=CMP70HX independent boot unlock
DefaultDependencies=no
After=local-fs.target systemd-modules-load.service systemd-udev-trigger.service
Before=sysinit.target shutdown.target
Conflicts=shutdown.target
RequiresMountsFor={kit.PREFIX} {kit.STATE}

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={interpreter} {helper} boot
ExecStopPost={interpreter} {helper} boot-failure
TimeoutStartSec={max(480, 180 * cards + 300)}
TimeoutStopSec=30
KillMode=control-group
UMask=0077

[Install]
WantedBy=sysinit.target
""".encode()
    kit.require(backend == "openrc", "Unknown init system")
    return f"""#!/sbin/openrc-run
# CMP70HX independent boot unlock
description="CMP70HX independent boot unlock"

depend() {{
    need localmount
    after modules udev-trigger
}}

start() {{
    ebegin "Applying CMP70HX unlock"
    {interpreter} {helper} boot
    eend $?
}}

stop() {{
    # Stopping the completed boot job does not unload a live GPU driver.
    return 0
}}
""".encode()


def enable():
    kit.require(kit.standalone_installation(), "Independent startup requires a standalone installation")
    backend = detect()
    config = kit.load(kit.CONFIG)
    data = json.loads((kit.PREFIX / "install.json").read_text())
    path = kit.SYSTEMD / kit.UNIT if backend == "systemd" else OPENRC_SCRIPT
    payload = content(backend, sys.executable, len(config["cards"]))
    kit.require(not path.is_symlink() and (not path.exists() or path.read_bytes() == payload),
                "Existing boot file has different content and was preserved: " + str(path))
    kit.require(data.get("boot_backend", backend) == backend, "Existing boot backend differs; remove it first")
    data.update(boot_backend=backend, boot_state="pending")
    kit.write_json(kit.PREFIX / "install.json", data)
    kit.atomic_write(path, payload)
    if backend == "systemd":
        kit.run("systemctl", "daemon-reload")
        kit.run("systemd-analyze", "verify", kit.UNIT)
        kit.run("systemctl", "enable", kit.UNIT)
        kit.require(kit.output("systemctl", "is-enabled", kit.UNIT) == "enabled", "Boot unit not enabled")
    else:
        path.chmod(0o755)
        kit.run("rc-update", "add", "cmp70hx-unlock", "boot")
        kit.require(OPENRC_RUNLEVEL.is_symlink() and OPENRC_RUNLEVEL.resolve() == path.resolve(),
                    "OpenRC boot link not created")
    data["boot_state"] = "enabled"
    kit.write_json(kit.PREFIX / "install.json", data)
    print("Automatic startup enabled:", backend)


def detach_openrc():
    if not OPENRC_SCRIPT.exists() and not OPENRC_SCRIPT.is_symlink():
        kit.require(not OPENRC_RUNLEVEL.is_symlink(), "Orphan OpenRC boot link; inspect before removal")
        return
    kit.require(not OPENRC_SCRIPT.is_symlink(), "Preserving symlinked OpenRC script")
    text = OPENRC_SCRIPT.read_text()
    kit.require("# CMP70HX independent boot unlock" in text and str(kit.PREFIX / "cmp70hx.py") in text,
                "Preserving unrecognized OpenRC script")
    kit.require(not OPENRC_RUNLEVEL.exists() or
                (OPENRC_RUNLEVEL.is_symlink() and OPENRC_RUNLEVEL.resolve() == OPENRC_SCRIPT.resolve()),
                "Preserving changed OpenRC boot link")
    archive = kit.STATE / ("detached-openrc-" + str(time.time_ns()))
    kit.atomic_write(archive / OPENRC_SCRIPT.name, OPENRC_SCRIPT.read_bytes())
    # Remove only the exact boot link created by this installer. No application
    # service or other runlevel is modified.
    if OPENRC_RUNLEVEL.is_symlink(): OPENRC_RUNLEVEL.unlink()
    OPENRC_SCRIPT.unlink()


def disable():
    kit.require(kit.standalone_installation(), "Use the recovery procedure for the older systemd integration")
    detach_openrc()
    kit.detach_integration()
    data = json.loads((kit.PREFIX / "install.json").read_text())
    data["boot_state"] = "disabled"
    kit.write_json(kit.PREFIX / "install.json", data)
    print("Automatic startup removed. Current GPU state is unchanged until reboot/reset.")


def status():
    data = json.loads((kit.PREFIX / "install.json").read_text())
    print(json.dumps({k: data.get(k) for k in ("integration", "boot_backend", "boot_state")}, indent=2))
    if data.get("boot_backend") == "systemd":
        kit.run("systemctl", "is-enabled", kit.UNIT, check=False)
    elif data.get("boot_backend") == "openrc":
        print("OpenRC boot link present:", OPENRC_RUNLEVEL.is_symlink() and OPENRC_RUNLEVEL.resolve() == OPENRC_SCRIPT.resolve())
