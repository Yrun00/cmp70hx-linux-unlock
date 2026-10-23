"""Independent startup lifecycle; host init and GPU calls are mocked."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import cmp70hx as kit
import cmp70hx_boot as boot
from test_kit import config


class Startup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        for name, value in dict(PREFIX=self.root / "installation", CONFIG=self.root / "config.json",
                                STATE=self.root / "state", SYSTEMD=self.root / "systemd", DISABLED=self.root / "disabled",
                                READY=self.root / "ready.json").items():
            self.stack.enter_context(patch.object(kit, name, value))
        self.script = self.root / "init.d/cmp70hx-unlock"
        self.link = self.root / "runlevels/boot/cmp70hx-unlock"
        self.stack.enter_context(patch.object(boot, "OPENRC_SCRIPT", self.script))
        self.stack.enter_context(patch.object(boot, "OPENRC_RUNLEVEL", self.link))
        self.stack.enter_context(patch.object(boot.sys, "executable", "/usr/bin/python3"))
        self.stack.enter_context(patch.object(kit, "boot_id", return_value="test-boot"))
        c = config(); c["services"] = []
        kit.write_json(kit.CONFIG, c)
        kit.write_json(kit.PREFIX / "install.json", dict(integration="standalone", mme=False))
        self.calls = []
        self.stack.enter_context(patch.object(kit, "run", side_effect=self.command))
        self.stack.enter_context(patch.object(kit, "output", return_value="enabled"))

    def command(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ("rc-update", "add"):
            self.link.parent.mkdir(parents=True, exist_ok=True)
            if not self.link.is_symlink(): self.link.symlink_to(self.script)
        return subprocess.CompletedProcess(args, 0, "")

    def test_systemd_early_boot_order_without_application_dependencies(self):
        with patch.object(boot, "detect", return_value="systemd"): boot.enable()
        text = (kit.SYSTEMD / kit.UNIT).read_text()
        self.assertIn("DefaultDependencies=no", text)
        self.assertIn("Before=sysinit.target shutdown.target", text)
        self.assertIn("WantedBy=sysinit.target", text)
        self.assertIn("cmp70hx.py boot", text)
        self.assertIn("ExecStopPost=/usr/bin/python3", text)
        self.assertIn("cmp70hx.py boot-failure", text)
        self.assertIn("KillMode=control-group", text)
        self.assertNotIn("display-manager", text)
        self.assertEqual(json.loads((kit.PREFIX / "install.json").read_text())["boot_state"], "enabled")
        boot.disable()
        self.assertFalse((kit.SYSTEMD / kit.UNIT).exists())

    def test_failed_enable_can_resume_and_does_not_claim_success(self):
        with patch.object(boot, "detect", return_value="systemd"):
            with patch.object(kit, "run", side_effect=RuntimeError("enable failed")):
                with self.assertRaises(RuntimeError): boot.enable()
            self.assertEqual(json.loads((kit.PREFIX / "install.json").read_text())["boot_state"], "pending")
            boot.enable()
        self.assertEqual(json.loads((kit.PREFIX / "install.json").read_text())["boot_state"], "enabled")

    @unittest.skipUnless(os.name == "posix", "Linux symlinks")
    def test_openrc_boot_runlevel_and_removal_without_systemd(self):
        with patch.object(boot, "detect", return_value="openrc"):
            boot.enable(); boot.enable()
        self.assertTrue(self.link.is_symlink())
        self.assertIn("need localmount", self.script.read_text())
        boot.disable()
        self.assertFalse(self.link.is_symlink())
        self.assertFalse(self.script.exists())
        self.assertFalse(any(c[0] == "systemctl" for c in self.calls))
        self.assertTrue(list(kit.STATE.glob("detached-openrc-*/*")))

    def test_foreign_unit_is_preserved(self):
        kit.atomic_write(kit.SYSTEMD / kit.UNIT, b"foreign content")
        with patch.object(boot, "detect", return_value="systemd"):
            with self.assertRaisesRegex(RuntimeError, "preserved"): boot.enable()
        self.assertEqual((kit.SYSTEMD / kit.UNIT).read_bytes(), b"foreign content")
        self.assertEqual(self.calls, [])

    def test_disable_flag_prevents_all_gpu_and_mme_work_at_boot(self):
        kit.DISABLED.touch()
        with patch.object(kit, "apply") as apply, patch.object(kit, "mme") as mme:
            kit.boot(None)
        apply.assert_not_called(); mme.assert_not_called()

    def test_boot_applies_kernel_then_configured_mme(self):
        kit.write_json(kit.PREFIX / "install.json", dict(integration="standalone", mme=True, lib_dir="/usr/lib64"))
        calls = []
        (kit.PREFIX / "nvidia.ko").write_bytes(b"fixture")
        def kernel(args):
            calls.append("kernel")
            kit.write_json(kit.READY, dict(boot_id="test-boot", status="success", started=123))
        def mme(args):
            calls.append((args.action, args.lib_dir))
            data=json.loads((kit.PREFIX / "install.json").read_text())
            data["mme_state"]="complete"
            kit.write_json(kit.PREFIX / "install.json", data)
        with patch.object(kit, "apply", side_effect=kernel), patch.object(kit, "mme", side_effect=mme), \
             patch.object(kit, "verify_mme", return_value={"files": []}):
            kit.boot(None)
        self.assertEqual(calls, ["kernel", ("install", "/usr/lib64")])
        self.assertEqual(json.loads((kit.PREFIX / "install.json").read_text())["mme_state"], "complete")
        result=json.loads((kit.STATE / "boot-result.json").read_text())
        self.assertEqual(result["kernel_started"],123)
        self.assertEqual(result["module_sha256"],kit.sha(kit.PREFIX / "nvidia.ko"))

    def test_failed_mme_invalidates_boot_success_receipt(self):
        kit.write_json(kit.PREFIX / "install.json", dict(integration="standalone", mme=True, lib_dir="/usr/lib64"))
        def kernel(args):
            kit.write_json(kit.READY, dict(boot_id="test-boot", status="success"))
        with patch.object(kit, "apply", side_effect=kernel), patch.object(kit, "mme", side_effect=RuntimeError("MME failed")):
            with self.assertRaisesRegex(RuntimeError, "MME failed"): kit.boot(None)
        self.assertFalse(kit.READY.exists())
        self.assertTrue(kit.DISABLED.exists())
        self.assertEqual(json.loads((kit.STATE / "boot-result.json").read_text())["status"], "failed")

    def test_corrupt_previous_result_disables_automatic_retry(self):
        kit.atomic_write(kit.STATE / "boot-result.json", b"broken json")
        with patch.object(kit, "apply") as apply:
            with self.assertRaises(ValueError): kit.boot(None)
        apply.assert_not_called()
        self.assertTrue(kit.DISABLED.exists())

    def test_failed_kernel_boot_does_not_apply_mme(self):
        with patch.object(kit, "apply", side_effect=RuntimeError("GPU failure")), patch.object(kit, "mme") as mme:
            with self.assertRaises(RuntimeError): kit.boot(None)
        mme.assert_not_called()
        self.assertTrue(kit.DISABLED.exists())
        self.assertEqual(json.loads((kit.STATE / "boot-result.json").read_text())["status"], "failed")

    def test_subprocess_timeout_invalidates_readiness_and_disables_retry(self):
        kit.write_json(kit.READY,dict(status='success',boot_id='test-boot'))
        with patch.object(kit,'apply',side_effect=subprocess.TimeoutExpired('GPU initialization',100)) as apply:
            with self.assertRaises(subprocess.TimeoutExpired):kit.boot(None)
            self.assertFalse(kit.READY.exists())
            self.assertTrue(kit.DISABLED.exists())
            result=json.loads((kit.STATE/'boot-result.json').read_text())
            self.assertEqual(result['status'],'failed')
            kit.boot(None)
            apply.assert_called_once()

    def test_interrupted_previous_boot_blocks_automatic_retry(self):
        kit.write_json(kit.STATE / "boot-result.json", dict(status="running", boot_id="previous-boot"))
        with patch.object(kit, "apply") as apply:
            with self.assertRaisesRegex(RuntimeError, "interrupted"): kit.boot(None)
        apply.assert_not_called()
        self.assertTrue(kit.DISABLED.exists())

    def test_mme_restore_remains_disabled_on_next_boot(self):
        import cmp_mme_manage
        kit.write_json(kit.PREFIX / "install.json", dict(integration="standalone", mme=True, lib_dir="/usr/lib64"))
        with patch.object(kit, "root_required"), patch.object(cmp_mme_manage.Manager, "inspect",
                return_value={"files": [{"state": "original"}, {"state": "original"}]}):
            kit.mme(argparse.Namespace(action="restore"))
            data = json.loads((kit.PREFIX / "install.json").read_text())
            self.assertFalse(data["mme_enabled"])
            self.assertTrue(data["mme"])
            (kit.PREFIX / "nvidia.ko").write_bytes(b"module")
            def kernel(args):
                kit.write_json(kit.READY, dict(boot_id="test-boot", status="success", started=123))
            with patch.object(kit, "apply", side_effect=kernel), patch.object(kit, "mme") as mme:
                kit.boot(None)
            mme.assert_not_called()
        self.assertEqual(json.loads((kit.STATE / "boot-result.json").read_text())["status"], "success")

    def test_failed_mme_restore_still_persists_disabled_intent(self):
        kit.write_json(kit.PREFIX / "install.json", dict(integration="standalone", mme=True, lib_dir="/usr/lib64"))
        with patch.object(kit, "root_required"), patch.object(kit, "run", side_effect=RuntimeError("restore failed")):
            with self.assertRaises(RuntimeError): kit.mme(argparse.Namespace(action="restore"))
        data = json.loads((kit.PREFIX / "install.json").read_text())
        self.assertFalse(data["mme_enabled"])
        self.assertEqual(data["mme_state"], "restore-pending")

    @unittest.skipUnless(sys.platform == "linux", "Linux process signals")
    def test_real_signals_and_stop_post_with_no_gpu_access(self):
        script = '''import pathlib, sys, time
sys.path.insert(0, sys.argv[1])
import cmp70hx as kit
root = pathlib.Path(sys.argv[2])
kit.STATE = root / "state"
kit.READY = root / "ready.json"
kit.DISABLED = root / "disabled"
kit.standalone_installation = lambda: True
kit.boot_id = lambda: "test-boot"
def apply(args):
    kit.write_json(kit.READY, {"status": "success"})
    (root / "waiting").touch()
    time.sleep(30)
kit.apply = apply
kit.boot(None)
'''
        for signum in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=signum):
                kit.DISABLED.unlink(missing_ok=True)
                (self.root / "waiting").unlink(missing_ok=True)
                child = subprocess.Popen([sys.executable, "-c", script, str(Path(kit.__file__).parent), str(self.root)],
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                try:
                    deadline = time.monotonic() + 5
                    while not (self.root / "waiting").exists() and child.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue((self.root / "waiting").exists())
                    child.send_signal(signum)
                    child.communicate(timeout=5)
                finally:
                    if child.poll() is None: child.kill(); child.communicate()
                result = json.loads((kit.STATE / "boot-result.json").read_text())
                if signum == signal.SIGTERM:
                    self.assertEqual(result["status"], "failed")
                    self.assertTrue(kit.DISABLED.exists())
                    self.assertFalse(kit.READY.exists())
                else:
                    self.assertEqual(result["status"], "running")
                with patch.dict(os.environ, SERVICE_RESULT="signal", EXIT_CODE="killed", EXIT_STATUS=str(signum)):
                    kit.boot_failure(None)
                self.assertFalse(kit.READY.exists())
                self.assertTrue(kit.DISABLED.exists())
                self.assertEqual(json.loads((kit.STATE / "boot-result.json").read_text())["service_result"], "signal")
        self.assertEqual(self.calls, [])

    def test_stop_post_success_leaves_completed_activation_untouched(self):
        kit.write_json(kit.READY, dict(status="success"))
        with patch.dict(os.environ, SERVICE_RESULT="success"): kit.boot_failure(None)
        self.assertTrue(kit.READY.exists())
        self.assertFalse(kit.DISABLED.exists())

    def test_stop_post_catches_cancelled_incomplete_start_even_with_success_result(self):
        kit.write_json(kit.STATE / "boot-result.json", dict(status="running", boot_id="test-boot"))
        with patch.dict(os.environ, SERVICE_RESULT="success"): kit.boot_failure(None)
        self.assertTrue(kit.DISABLED.exists())
        self.assertEqual(json.loads((kit.STATE / "boot-result.json").read_text())["status"], "failed")

    def test_boot_interpreter_rejects_ephemeral_or_injected_path(self):
        for interpreter in ("/home/user/venv/python", "/opt/hostedtoolcache/Python/3.12.3/x64/bin/python",
                            "/usr/bin/python%h", "/usr/bin/python;reboot"):
            with patch.object(kit.sys, "executable", interpreter):
                with self.assertRaises(RuntimeError): boot.content("systemd", interpreter, 1)


if __name__ == "__main__":
    unittest.main()
