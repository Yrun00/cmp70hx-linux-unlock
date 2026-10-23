# Manual commands

Use these instead of the wizard for scripted setup or module signing. Run from
the extracted kit. PYZ supports the same arguments.

The wizard uses reboot-based preparation. For the same behavior after the build
below, run `sudo python3 cmp70hx.py prepare-reboot --build build-host
--ack-shared-driver --lib-dir /your/library/directory`, then reboot normally.
Omit `--lib-dir` to skip MME. This stages files/startup without closing applications
or applying GPU changes to the current session. The direct live-activation method
later in this document is an advanced alternative and requires unused GPU devices.

## Discovery and configuration

```bash
python3 cmp70hx.py discover
```

This reads PCI metadata only. Each object describes one card:

```json
{
  "bdf": "0000:01:00.0",
  "ids": {"vendor": "0x10de", "device": "0x248a", "subsystem_vendor": "0x10de", "subsystem_device": "0x248a"},
  "width": 4,
  "driver": "nvidia",
  "supported": true
}
```

Copy `bdf` exactly, including the leading `0000:`. Choose only objects with
`supported: true`. `width: 4` means four connected lanes, not PCIe generation 4.
An empty list means no matching CMP70HX was discovered.

Example for one card (replace the address with yours):

```bash
python3 cmp70hx.py configure --cards 0000:01:00.0 --gfx 0000:01:00.0 --pcie 0000:01:00.0 --output host.json
```

Compute is enabled on every `--cards` target. `--gfx` selects graphics targets
and defaults to `all`. `--pcie` selects Gen2 targets and defaults to none.
For two compute cards, with graphics/PCIe only on the first:

```bash
python3 cmp70hx.py configure --cards 0000:01:00.0,0000:03:00.0 --gfx 0000:01:00.0 --pcie 0000:01:00.0 --output host.json
```

For compute only, omit `--pcie` and pass `--gfx ""`. No service configuration is
needed. `host.json` is created, without changing the GPU; inspect the `cards`
entries before building. An existing output file is preserved: use a new filename
and supply the same filename to the following build command.

## Build and signing

Download the exact official source archive and verify it:

```bash
curl -fLO https://download.nvidia.com/XFree86/NVIDIA-kernel-module-source/NVIDIA-kernel-module-source-610.43.03.tar.xz
echo '7e118923c7a23edc36114d63273a46e3e04e9af98695a42203e7ac2dfe9fc1dc  NVIDIA-kernel-module-source-610.43.03.tar.xz' | sha256sum -c -
python3 cmp70hx.py build --config host.json --archive NVIDIA-kernel-module-source-610.43.03.tar.xz --output build-host --jobs 2
```

Run the build without sudo. Each build needs a new output directory. The kernel
headers must match `uname -r`; install them with your distribution's package manager.
`build-host` contains `nvidia.ko`, the configuration, hashes and `host.patch`.

For Secure Boot, sign `build-host/nvidia.ko` using your kernel's `sign-file` tool
and an already trusted/enrolled key, following your distribution's module-signing
procedure. Keep the private key outside the kit. Then update its expected hash:

```bash
python3 cmp70hx.py seal --build build-host
```

## Standalone installation

Close NVIDIA applications. If the desktop uses NVIDIA, log out and use a text
console or SSH. The tool will refuse to force-unload a busy driver.

```bash
sudo python3 cmp70hx.py install --build build-host --ack-shared-driver --standalone
```

If a previous uninstall left `/etc/cmp70hx.disabled`, remove that flag only when
ready to activate: `sudo unlink /etc/cmp70hx.disabled`. Then:

```bash
sudo python3 /usr/local/lib/cmp70hx/cmp70hx.py apply --ack-shared-driver
sudo python3 /usr/local/lib/cmp70hx/cmp70hx.py status --verify
```

After successful verification, patch MME with the actual library directory:

```bash
sudo python3 /usr/local/lib/cmp70hx/cmp70hx.py mme install --lib-dir /usr/lib/x86_64-linux-gnu
sudo python3 /usr/local/lib/cmp70hx/cmp70hx.py mme status
```

The directory above is a Debian/Ubuntu example; use your own directory containing
both regular versioned files. The standalone installation requires no systemd,
has no application dependencies and adds no boot hook. Repeat `apply` after reboot
before using NVIDIA. Rebuild after a kernel change. See [recovery](RECOVERY.md).

To make this installation persistent (the wizard does this automatically):

```bash
sudo python3 /usr/local/lib/cmp70hx/cmp70hx.py autostart enable
sudo python3 /usr/local/lib/cmp70hx/cmp70hx.py autostart status
```

This creates an independent early-boot job using systemd or OpenRC. At subsequent
boots it reapplies the kernel unlock and configured MME patch without managing
applications. `autostart disable` removes the boot job while retaining the current
installation; full uninstall removes both. Other init systems can use manual
activation, but do not yet have an automated startup adapter.

## Optional systemd installation

For an administrator who explicitly wants systemd boot integration, use the same
build and omit `--standalone` at installation. Do this on a fresh installation;
uninstall an existing standalone setup before switching modes. Start and verify:

```bash
sudo python3 cmp70hx.py install --build build-host --ack-shared-driver
sudo systemctl start cmp70hx-unlock.service
systemctl show cmp70hx-unlock.service -p ActiveState -p SubState -p Result
```

Wait for `active / exited / success`, then run `status --verify` as above. Enable
the unit only after successful validation: `sudo systemctl enable cmp70hx-unlock.service`.
In this mode, `mme install` also creates its separate systemd boot unit.

The installer orders its unit before common graphics startup units. Administrators
must ensure all NVIDIA users wait until activation completes. The optional advanced
`configure --services name.service,other.service` parameter creates `Requires/After`
dependencies for explicitly chosen existing units; it is not used by the wizard.
Failed unlock then holds those units stopped. Never include SSH/recovery services.
Uninstall removes these kit-owned dependencies. Review [recovery](RECOVERY.md) before
enabling unattended boot. No application-specific integration is required.
