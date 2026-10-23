# Installation with the wizard

Run every command here on the Linux computer containing the CMP70HX, in a terminal.
Install NVIDIA OPEN 610.43.03 and your distribution's development headers for the
running kernel first. The wizard checks requirements; it does not install packages.
Keep your downloaded PYZ or extracted kit for recovery.

## 1. Start

For the single downloaded file:

```bash
python3 cmp70hx-linux-0.2.5.pyz
```

For extracted sources, open the directory containing `cmp70hx.py` and run:

```bash
python3 cmp70hx.py
```

Do not put `sudo` before this command. Building runs under your account. Later,
sudo may request your login password; password characters are not displayed.
Press Enter to accept an answer shown in square brackets.

## 2. Choose cards by number

Example output for a computer with two cards:

```text
Number  PCI address      Link width  Driver
     1  0000:01:00.0     x4          nvidia
     2  0000:03:00.0     x1          nvidia
```

These addresses are examples. Use the list on your own computer.

| Field | Meaning | What to do |
|---|---|---|
| Number | Position in this menu | Type this number when selecting a card |
| PCI address | Address of the physical card on this motherboard | Use it to distinguish slots/cards |
| Link width | Connected PCIe lanes: x4 means four, x1 means one | A narrow riser often reports x1; this is not the PCIe generation |
| Driver | Driver bound to that card | Normally `nvidia`; investigate `none`/`nouveau` before proceeding |

Cards with unsupported board identifiers are shown separately and cannot be selected.

For a **single card**, answer:

1. Compute/Tensor: `1`.
2. Graphics: `1`.
3. PCIe Gen2: `1` if this is the link you want to upgrade and its reported width
   is x1, x2 or x4. Otherwise answer `none` to leave the link unchanged.

For the **two-card example above**, to unlock compute on both and graphics/PCIe
only on the first card, answer:

1. Compute/Tensor: `all` (or `1,2`).
2. Graphics: `1`.
3. PCIe Gen2: `1`.

The second card's PCIe and graphics settings will stay unchanged. If you select a
subset for compute, the wizard prints a new numbered list for the following
questions. Use those displayed numbers. Do not infer physical slot identity from
lane width alone when several cards are installed.

`all` means every card in the current list. `none` skips the requested optional
feature. PCIe defaults to `none`, so you must explicitly choose it to enable it.

## 3. Source archive and build

At the archive prompt, either enter the path of your already downloaded exact
NVIDIA source `.tar.xz`, or press Enter and answer `yes` to the official download.
The checksum is checked in both cases.

The wizard then builds automatically. Compiler output appears directly in the
terminal and is also saved to `build.log`. If the compiler is quiet for about ten
seconds, the wizard prints an elapsed-time message so you know it is still waiting.
A failed build stops before installation and retains its complete log.

Your configuration, source copy and build are saved in a new directory under:

```text
~/.local/share/cmp70hx/setups/
```

You do not need to edit JSON or construct a `configure` command.

## 4. Review and select MME

Read the per-card summary: compute/Tensor, graphics and PCIe selection. The wizard
prints a resume command and also saves it as `RESUME.txt` inside the setup directory.
Keep that command to continue later if needed. Applications can remain open.
No GPU settings or live libraries are changed during this preparation.

The wizard lists detected library
directories. If exactly one is found, press Enter to use it. Otherwise enter the
actual directory containing **both** regular versioned files:

```text
libnvidia-eglcore.so.610.43.03
libnvidia-glcore.so.610.43.03
```

The selected exact-hash patch is applied during boot. Originals are saved for
restoration. Unknown library contents are refused before staging. This patch applies to system libraries used
by all GPUs. The path can differ between distributions; the wizard accepts a
custom directory. `skip` leaves MME unchanged and the completion message reports
that the fourth unlock was skipped.

## 5. Install and restart

The final screen lists what remains: install files, register automatic startup,
then apply and verify the unlock during boot. Choose:

- `REBOOT`: prepare the installation and request a normal system restart.
- `INSTALL`: prepare the installation while keeping applications open; restart
  normally when convenient.
- Enter: cancel, keeping the completed build for later.

Save documents before choosing `REBOOT`. The normal restart closes desktop/remote
sessions and applications. No manual process cleanup is required. If preparation
fails, the wizard does not reboot. Startup registration must succeed first.

If the desktop inhibits the restart, the installation remains prepared. Save
your work and restart through the desktop menu. Do not reinstall or resume setup
just because the reboot request was blocked.

After restarting, the early-boot job performs kernel activation, verification and
the selected MME patch, then ordinary startup continues. This can add several
minutes to the first boot. Automatic startup is retained for subsequent boots.

An earlier uninstall's disable flag is removed only after confirmed preparation
and successful startup registration. Power, voltage, memory offsets and application
configuration are left unchanged.

## 6. Check the result and use it after reboot

To check every configured unlock:

```bash
sudo python3 /usr/local/lib/cmp70hx/cmp70hx.py status --verify
```

The command must finish without `ERROR`. It checks current boot completion,
compute registers, graphics selection, the requested Gen2 link/width and both
MME library hashes when MME was selected. For detailed library status:

```bash
sudo python3 /usr/local/lib/cmp70hx/cmp70hx.py mme status
```

Both files should have state `patched`. Test a familiar application and check
that its outputs/images are correct. See [validation](VALIDATION.md) for test coverage.

The wizard enables automatic startup using systemd or OpenRC. You do not need to
run installation or activation commands after each reboot. On the first planned
reboot, verify that startup worked:

```bash
sudo python3 /usr/local/lib/cmp70hx/cmp70hx.py status --verify
sudo python3 /usr/local/lib/cmp70hx/cmp70hx.py autostart status
```

The boot job reapplies the kernel unlock and checks/reapplies MME if it was selected
during installation. An unknown library or incompatible kernel/driver makes it
stop with a recorded error. After a kernel/driver update, follow the rebuild
procedure in Recovery. No application-specific startup settings are required.

## 7. If a step fails

Stop at the reported step. Read the message and saved setup path, address the
named prerequisite, then use `RESUME.txt`. If boot activation failed, inspect
`/var/lib/cmp70hx/boot-result.json`, `/var/lib/cmp70hx/boot.log` and
`/var/lib/cmp70hx/last-run.json`. Automatic retries are disabled after a failed or
interrupted boot activation. Keep the disable flag until the cause is understood;
see Recovery. Do not repeatedly reboot to retry a failure.

To remove the installation, first block its next boot activation and reboot:

```bash
sudo touch /etc/cmp70hx.disabled
sudo reboot
```

After logging back in, close NVIDIA applications and use the downloaded kit:

At this point MME libraries can still be patched: the disable flag, removing
autostart and rebooting do not restore files on disk. Complete the uninstall below
to restore them too. To disable only MME while keeping the kernel unlock, use
`sudo python3 /usr/local/lib/cmp70hx/cmp70hx.py mme restore` with affected graphics
applications closed; see [Recovery](RECOVERY.md#mme-only-restore).

```bash
sudo python3 cmp70hx.py uninstall
```

For PYZ, substitute `cmp70hx-linux-0.2.5.pyz` for `cmp70hx.py`. Uninstall requires
the modified module to be absent, removes the boot job, restores known original
libraries and retains backups. It does not delete your applications. Complete
recovery and kernel updates are covered in [recovery](RECOVERY.md).

## Updating an earlier installation

Remove the previous installation using the recovery procedure, then start the
new wizard. Version 0.2.5 updates license headers in three driver templates without
changing executable driver code. Their source hashes changed, so the exact-provenance
check requires a fresh build when upgrading from an earlier version. A matching
0.2.5 build can be resumed while its running kernel still matches.
