# Validation and supported scope

## Evidence available in this release

The CPU suite exercises configuration, source generation, install rollback,
removal after a kernel change, MME transactions, boot receipts, systemd/OpenRC
file generation, interpreter selection, reboot refusal and packaging. A C test
executes the actual GPU-slot bounds helper. System/GPU actions are mocked;
these tests run without a CMP70HX. See [Development](DEVELOPMENT.md).

Earlier versions were exercised on two GA104 CMP70HX cards with device/subsystem
`10de:248a / 10de:248a`, Ubuntu 26.04.1, kernel `7.0.0-34-generic`, and NVIDIA
OPEN 610.43.03. Version 0.1.0 completed installation, correctness checks for all
four mechanisms, reboot persistence and uninstall. Version 0.2.1 completed a
user-operated wizard installation and boot activation on that stand.

Version 0.2.4 added signal/failure receipts, persistent MME disable policy,
privileged installer-code staging and library metadata preservation. Module/config
installation uses a private verified snapshot; diagnostic state is private, and
persistence-mode restoration is reported explicitly. Version 0.2.5 corrects CI's
interpreter, disable/restore documentation and source license headers. The complete
hardware build/boot/recovery lifecycle of the current release
has not yet been run. OpenRC, other distributions, single-card systems, different
PCI addresses, multiple PCIe/GFX targets, Gen2 x1/x2, Secure Boot, suspend/resume
and VM passthrough have no completed validation record in this release.

This archive contains no reproducible performance benchmark suite or raw
performance dataset. Historical lab speed figures are therefore omitted. Register
and library verification establish configured state; application correctness and
performance should be measured separately on the target machine.

## Check an installation

After reboot, capture these commands and their exit codes:

```bash
uname -r
sudo python3 /usr/local/lib/cmp70hx/cmp70hx.py status --verify
sudo python3 /usr/local/lib/cmp70hx/cmp70hx.py autostart status
```

When MME was selected:

```bash
sudo python3 /usr/local/lib/cmp70hx/cmp70hx.py mme status
```

`status --verify` (also spelled `--verify-all`) checks the current activation,
loaded module identity/policy, selected kernel registers and requested link state.
For boot-managed installations it requires successful completion of that boot
transaction. For configured MME it checks both exact patched library hashes.
The service runs while the machine boots; wait for it to complete before checking.

Retain `/var/lib/cmp70hx/boot-result.json`, `boot.log`, `last-run.json`, the
configuration and build metadata. Use fresh application processes after changing
libraries. Check actual images/numerical results as well as frame rate or throughput.
Review logs for personal information before posting them.

For a publishable comparison, record the application version/source, exact command,
GPU identity, workload size, clock/power settings, warm-up and repeat count, raw
per-run times and correctness results for both stock and unlocked operation.
Define any derived throughput formula and operation-count convention explicitly.

Complete a reboot verification and the [uninstall/recovery procedure](RECOVERY.md)
before reporting a full installation lifecycle as tested.
