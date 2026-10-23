# Recovery

## Independent installation (the wizard default)

The wizard installs an independent systemd/OpenRC boot job without application
dependencies. It reapplies the kernel unlock and configured MME patch at boot.
The wizard stages installation with running applications untouched, then requests
a restart only when the user chooses `REBOOT`. It records preparation separately
from the actual result of activation on the following boot.
Inspect `/var/lib/cmp70hx/last-run.json` after an activation failure. The helper
attempts stock-module recovery after an ordinary transition error. A manual `--standalone`
installation without `autostart enable` has no boot job.

For boot diagnostics use `sudo cat /var/lib/cmp70hx/boot-result.json` and
`sudo less /var/lib/cmp70hx/boot.log`. The state directory is private (`0700`),
and new diagnostic files/logs use `0600`.
Failure, or evidence of an interrupted previous activation, creates
`/etc/cmp70hx.disabled` to stop automatic retries. Normal startup can continue
without another unlock attempt. Investigate the reported cause before removing
this flag and scheduling another activation. The underlying activation receipt
and stock-driver recovery attempt remain in `last-run.json`. `status --verify`
requires a successful current-boot transaction, matching loaded driver/readbacks,
and both library hashes matching the selected MME policy (patched when enabled,
original after restore). A failed
MME boot stage invalidates readiness even if kernel activation succeeded.

The systemd startup timeout is `max(480, 180*N+300)` seconds: 8 minutes for one
card, 11 for two, and 29 for eight. These are maximum allowances, not normal
activation times. The job runs before `sysinit.target`; a stalled activation can
delay graphical login and SSH. Systemd allows another 30 seconds for stopping.

On SIGTERM the controller attempts to record the interruption and disable retries.
It starts no further GPU operations during termination. Systemd's separate
`ExecStopPost` handler also removes readiness and records failure after a killed
process, including SIGKILL. It performs filesystem cleanup only: the GPU, driver,
persistence mode and MME libraries may be left in an intermediate state.
Do not resume GPU applications after this failure; reboot with the disable flag.
If protected state remains unexpected, fully power off before recovery.

Power loss, an unresponsive kernel or failure to run/write the cleanup handler
can leave a `running` receipt. A subsequent activation detects it and disables
retries. If the machine cannot reach a login, use recovery mode or a live Linux
USB to create `etc/cmp70hx.disabled` on the installed system's root filesystem,
then boot normally and inspect the saved logs. Actual systemd timeout and hardware
recovery remain untested; process-signal tests use mocked GPU operations.

To remove an independent installation:

1. Close NVIDIA applications. Set `sudo touch /etc/cmp70hx.disabled` to block
   accidental activation, then reboot into the stock driver.
2. From the downloaded kit run `sudo python3 cmp70hx.py uninstall` (or substitute
   your downloaded PYZ). It verifies that the modified module is absent, removes
   the boot job, restores known original MME libraries and archives the installation.
   On OpenRC, removal needs no systemd commands.
3. Read any library-restoration warning and verify the current stock driver before
   opening GPU applications. If protected state remains unexpected after reboot,
   fully power off before further activation attempts.

Before a kernel/driver update, uninstall this way. After the update, build a new
configuration/module with the matching supported driver. A new wizard run removes
the retained disable flag only after successful preparation and boot registration.

For an interrupted standalone installation, the journal/retry/recover-install
procedure below is the same and does not require systemd. For MME-only restore,
the commands below also work without systemd. MME restore preserves the kernel
unlock's boot configuration and disables only the desired MME activation policy.
A failed boot-setup attempt can be resumed by
repeating `autostart enable`; conflicting foreign files are preserved.

## Systemd mode

The remaining boot-unit instructions apply to the optional systemd installation.

The clean distro module is the fallback. The kit never replaces it or builds an
initramfs. Save an independent SSH/TTY route before stopping a graphical session.

## Failure during activation

1. Stop new experiments and concurrent NVIDIA status probes.
2. Read `journalctl -u cmp70hx-unlock.service -b` and
   `/var/lib/cmp70hx/last-run.json`.
3. An open client causes refusal before module cycling. Close the client and
   review all configured consumer units. Forced module unloading is not used.
4. After an ordinary cycle error, the helper attempts to unload its module and load the
   stock `nvidia_drm`. It records whether that attempt succeeded. Consumer
   services stay stopped after a hardware-transition failure.
5. Persistence mode is disabled before the module cycle. Its original state is
   recorded in `persistence_before`; the restoration outcome is reported in
   `persistence_restore` (`pending`, `restored`, `failed` or `withheld`). `pending`
   after an interrupted process means restoration was not confirmed. Restoration is
   only attempted when the driver responds and reports the same GPU UUIDs.
6. Loading stock modules does not itself reset all protected register state.
   Set the disable flag and reboot. If the state remains unexpected, shut down
   and remove power before the next clean-driver verification.

If selected cards have a mixture of open and closed protected gates, activation
stops. Fully power off the machine before retrying and retain the failed log.

## Disable before reboot

The disable flag and `autostart disable` stop future kernel-unlock activation.
They leave already patched MME libraries on disk; rebooting does not restore those
files. To restore only MME, close affected graphics processes and follow
[MME-only restore](#mme-only-restore). To return the entire installation to stock,
complete [uninstall](#complete-uninstall--update) after the reboot below and verify
both original library hashes. The boot failure handler does not change libraries
automatically while the machine may be recovering.

```bash
sudo touch /etc/cmp70hx.disabled
sudo reboot
```

The boot unit exits without loading the modified module. Its `Requires`
dependents remain stopped, including the desktop if configured that way.
SSH must not be in the managed-consumer list. Removing `enable` alone is
insufficient because consumers can still request the unit.

## MME-only restore

Close affected graphics processes, then:

```bash
sudo python3 /usr/local/lib/cmp70hx/cmp70hx.py mme restore
sudo python3 /usr/local/lib/cmp70hx/cmp70hx.py mme status
```

Verify both original hashes; restart affected processes. Restore saves
`mme_enabled: false` before changing libraries, so MME stays off on following
boots. Its configuration and backups are retained; the kernel unlock stays enabled.
If restoration fails halfway, repeat `mme restore` after resolving the error.
Startup will refuse changed/partially restored libraries instead of repatching them.
To re-enable MME later, after the kernel unlock is ready, use the directory shown
in your installation's `lib_dir` (replace `YOUR_DIRECTORY` below):

```bash
sudo python3 /usr/local/lib/cmp70hx/cmp70hx.py mme install --lib-dir YOUR_DIRECTORY
```

Originals are under `/var/lib/cmp-mme/original/610.43.03/`. Unknown current
libraries are refused. Use the vendor's normal installer to restore a different
driver release; never force known offsets into unknown libraries.

## Complete uninstall / update

After booting with the disable flag and verifying that the modified module is
not loaded:

```bash
sudo python3 cmp70hx.py uninstall
```

This restores supported MME libraries, disables/deletes the kit's units and exact
consumer drop-ins, and archives its installation under `/var/lib/cmp70hx/`.
It retains the disable flag and does not start consumers.

Use the current downloaded script, especially when removing an older installation.
Cleanup does not require the previous kernel, stock-module path or saved config.
It requires the modified module to be absent. Library restoration failures are
reported separately after removing the desktop/service dependencies. Backups and
the removal report remain under `/var/lib/cmp70hx/`.

## Kernel or driver update

1. Create `/etc/cmp70hx.disabled` before rebooting into the new kernel.
2. Boot the new kernel. If you already upgraded and unlock failed, create the
   disable flag now. A reboot is required only if the modified module is loaded.
3. From the current downloaded kit, run `sudo python3 cmp70hx.py uninstall`.
   This removes the old unlock dependencies even if the NVIDIA driver is missing
   or the old kernel directory no longer exists.
4. Install/verify the stock driver for the running kernel before restarting the
   desktop or other GPU applications. Unknown MME libraries are left untouched;
   follow any restoration warning using the vendor installer if needed.
5. Generate a new config and build. Install the new kit, then remove
   `/etc/cmp70hx.disabled` only when ready to activate.

## Interrupted installation

Normal install errors roll back created files. An abrupt termination leaves
`/var/lib/cmp70hx/pending-install.json`; repeating `install` recovers it first.
Activation is blocked while this journal exists.

To perform cleanup separately, from the current downloaded kit:

```bash
sudo python3 cmp70hx.py recover-install
```

This also handles incomplete older installations without a journal, config or
installation manifest. Recognized files are removed or archived; changed files
are preserved for inspection. A complete installation should use `uninstall`.

Interrupted MME installation can be resumed by repeating `mme install` with the
same `--lib-dir`. It retains verified originals and checks both current inputs
before patching.
