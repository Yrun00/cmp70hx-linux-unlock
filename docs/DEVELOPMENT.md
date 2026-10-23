# Development

## Tests

Run from the source directory on Linux with Python 3.12+ and a C compiler (`cc`):

```bash
/usr/bin/python3 -m unittest discover -v
```

Tests use temporary files and mocked GPU operations. They cover configuration,
target selection, source generation, readiness checks, activation/recovery,
interrupted installation, removal after a kernel change, concurrent-command
exclusion and MME apply/restore. Filesystem transaction and lifecycle tests require Linux.
Wizard tests cover numbered/subset selection, cancellation, changed-build refusal,
live build output/heartbeat, reboot consent, staged preparation and preserved-build
resume. Standalone lifecycle
tests assert that installation, rollback, MME and removal make no systemd calls.
Startup tests cover early systemd ordering, OpenRC boot registration/removal,
retry after a failed enable, the disable flag and kernel-before-MME ordering.
Boot failure/interruption tests verify that automatic retries are disabled,
failed MME invalidates readiness, and both configured library hashes are checked.
Signal tests terminate a separate Python process with SIGTERM/SIGKILL and invoke
the failure hook using simulated systemd environment values. They do not run a
real systemd timeout or unload a driver. MME tests preserve real Linux xattrs and
POSIX ACLs, and exercise metadata-write refusal/rollback.
A small C harness executes the actual overlay's GPU-slot helper for valid and
out-of-range indices. It does not build or load a kernel module.
CI also packages the sources and checks the single-file entry point.
Use the persistent system interpreter for this suite; virtual environments and
GitHub's toolcache Python exercise an intentionally refused installation path.
CI pins Ubuntu 24.04 and explicitly invokes `/usr/bin/python3` for every step.

## Source layout

| Path | Purpose |
|---|---|
| `cmp70hx.py` | Configuration, build, installation and driver lifecycle |
| `cmp70hx_wizard.py` | Interactive standalone installation |
| `cmp70hx_boot.py` | Independent systemd/OpenRC startup |
| `cmp_mme_manage.py` | Exact-hash userspace patching and restoration |
| `overlay/` | Modified OPEN 610.43.03 source templates |
| `PROVENANCE.json` | Source archive and template hashes |
| `test_*.py` | CPU tests |
| `package.py` | Source archives and Python zipapp |

The builder generates device guards from `host.json` in `nv.c` and
`cmp70_port.h`. Raw templates contain `#error` directives and must not be built
directly; generation removes these after inserting the selected policy.
The remaining four source files implement the protected-write path.
Review the generated `host.patch` when changing the target policy.

## Packaging

```bash
python3 package.py ../dist-0.2.6
```

Use a new output directory. The command creates ZIP, TAR.GZ, PYZ and
`SHA256SUMS`; the source archives also contain an internal file manifest.
An exact file allowlist includes sources, public documentation and license texts.
Vendor binaries, host configurations and build outputs are excluded. Additional
files placed under `docs/`, `overlay/` or `.github/` are not implicitly packaged.

Source files use LF line endings, enforced by `.gitattributes`; packaging refuses
CRLF sources. Archive timestamps, ownership and permissions are fixed. Rebuilding
the same sources with the same Python/zlib versions produces byte-identical
artifacts. GitHub's automatically generated source archives have their own
container metadata; compare their extracted files using the internal manifest.

For publication, commit the internal `SHA256SUMS` from the source ZIP alongside
the exact packaged sources, then rebuild from that commit into a new directory.
CI checks this manifest against the checkout. Upload the three rebuilt artifacts
and their external `SHA256SUMS`. After uploading, download them and check both
the external manifest and byte equality of the extracted sources against the tag.

## Installation trust boundary

Use a trusted copy of the kit and build in your own directory. The wizard reads
the installer sources into memory, passes them to an isolated system Python via
stdin, and checks the transfer digest before executing them in a private temporary
directory owned by the privileged process. Root never launches the setup directory's
Python script. Installation also copies module/configuration inputs into a private
root-owned snapshot and validates that snapshot before installing its contents.
Tests replace the user-side files after transfer preparation and check that they
are not executed or installed.

These hashes establish consistency, not publisher identity. A compromised download
or already-compromised user session remains outside this installer's trust model.
The advanced manual `sudo python3 cmp70hx.py ...` interface executes the explicitly
chosen local script; review and protect that copy before running it.

Library replacement preserves and reads back ownership, mode, POSIX ACLs and
extended attributes (including an existing SELinux label) before replacing the
live inode. Metadata-copy failure aborts replacement and rolls back earlier files.
IMA/EVM-signed libraries are explicitly refused, since changing content invalidates
their integrity metadata. SELinux policy enforcement on a real target distribution
still requires validation; a policy denial is an error, not permission to strip labels.

## Reporting hardware results

Include GPU PCI/subsystem IDs, kernel/driver versions, selected features, module
and library hashes, and the relevant service log. Report correctness checks,
performance conditions, reboot behavior and recovery separately. Review logs
for hostnames and other personal information before sharing them.

See [validation](VALIDATION.md) for the current hardware coverage.

The OpenRC adapter follows the [upstream service-script guide](https://github.com/OpenRC/openrc/blob/master/service-script-guide.md).
It uses a synchronous boot-runlevel job; the systemd adapter runs before
`sysinit.target` with explicit early-boot dependencies. Neither adapter adds
application-specific ordering or service-management commands.
