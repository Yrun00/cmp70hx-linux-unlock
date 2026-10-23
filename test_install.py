"""Installation/removal failure tests; all system and GPU calls are mocked."""
import argparse
import contextlib
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import cmp70hx as kit
from test_kit import config


class Installation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.temp.name)
        for name, value in dict(PREFIX=self.root / "prefix", CONFIG=self.root / "config.json",
                                STATE=self.root / "state", SYSTEMD=self.root / "units",
                                READY=self.root / "ready.json", DISABLED=self.root / "disabled").items():
            self.stack.enter_context(patch.object(kit, name, value))
        self.stack.enter_context(patch.object(kit, "root_required"))
        self.stack.enter_context(patch.object(kit, "ensure_inactive_module"))
        self.stack.enter_context(patch.object(kit, "check_hardware"))
        self.c = config(); self.c["services"] = ["display-manager.service"]
        self.stack.enter_context(patch.object(kit.platform, "release", return_value=self.c["kernel"]))
        self.build = self.root / "build"; self.build.mkdir()
        kit.write_json(self.build / "config.json", self.c)
        (self.build / "nvidia.ko").write_bytes(b"module fixture")
        kit.write_json(self.build / "build.json", dict(module_sha256=kit.sha(self.build / "nvidia.ko"),
                                                      config_sha256=kit.sha(self.build / "config.json")))
        self.args = argparse.Namespace(build=str(self.build), ack_shared_driver=True)
        self.calls = []
        self.failure = None
        self.stack.enter_context(patch.object(kit, "run", side_effect=self.command))
        self.stack.enter_context(patch.object(kit, "output", side_effect=self.output))

    def command(self, *args, **kwargs):
        self.calls.append(args)
        if self.failure and self.failure(args):
            raise RuntimeError("injected command failure")
        text = "not-found\n" if args[:2] == ("systemctl", "show") else ""
        return subprocess.CompletedProcess(args, 0, text)

    def output(self, *args):
        self.calls.append(args)
        if args[0] == "systemctl": return "loaded"
        if "-n" in args: return self.c["stock_module"]
        if "license" in args: return "Dual MIT/GPL"
        return kit.VERSION

    def dropin(self):
        return kit.SYSTEMD / "display-manager.service.d/90-cmp70hx.conf"

    def test_second_file_failure_rolls_back_and_retry_succeeds(self):
        real = kit.atomic_write
        def fail(path, data):
            if path == kit.PREFIX / "cmp_mme_manage.py": raise OSError("injected copy failure")
            return real(path, data)
        with patch.object(kit, "atomic_write", side_effect=fail):
            with self.assertRaisesRegex(OSError, "injected"):
                kit.install(self.args)
        self.assertFalse(kit.PREFIX.exists())
        self.assertFalse(kit.CONFIG.exists())
        self.assertFalse((kit.STATE / "pending-install.json").exists())
        kit.install(self.args)
        self.assertTrue(self.dropin().exists())

    def test_source_changes_after_snapshot_do_not_change_installed_bytes(self):
        module=(self.build/'nvidia.ko').read_bytes()
        config_bytes=(self.build/'config.json').read_bytes()
        def mutate_source(config):
            (self.build/'nvidia.ko').write_bytes(b'changed after validation')
            (self.build/'config.json').write_text('{}')
        with patch.object(kit,'check_hardware',side_effect=mutate_source):
            kit.install(self.args)
        self.assertEqual((kit.PREFIX/'nvidia.ko').read_bytes(),module)
        self.assertEqual(kit.CONFIG.read_bytes(),config_bytes)
        self.assertFalse(list(kit.STATE.glob('install-snapshot-*')))

    def test_corruption_during_snapshot_copy_is_refused_before_install(self):
        original=kit.read_regular_file
        def corrupt(path):
            return b'corrupt module' if Path(path).name=='nvidia.ko' else original(path)
        with patch.object(kit,'read_regular_file',side_effect=corrupt):
            with self.assertRaisesRegex(RuntimeError,'hash mismatch in private snapshot'):
                kit.install(self.args)
        self.assertFalse(kit.PREFIX.exists())
        self.assertFalse(list(kit.STATE.glob('install-snapshot-*')))

    @unittest.skipUnless(os.name=='posix','Linux permissions and symlinks')
    def test_snapshot_is_private_and_input_links_are_rejected(self):
        import stat
        with kit.installation_snapshot(self.build) as folder:
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode),0o700)
            self.assertEqual(folder.stat().st_uid,os.geteuid())
            self.assertTrue(all(stat.S_IMODE(p.stat().st_mode)==0o600 for p in folder.iterdir()))
        module=self.build/'nvidia.ko'; saved=self.build/'original.ko'
        module.rename(saved); module.symlink_to(saved)
        with self.assertRaises(OSError): kit.install(self.args)
        self.assertFalse(kit.PREFIX.exists())

    def test_reboot_preparation_uses_the_same_private_snapshot(self):
        self.standalone(); self.args.lib_dir=None
        import cmp70hx_boot
        module=(self.build/'nvidia.ko').read_bytes()
        def mutate_source(config): (self.build/'nvidia.ko').write_bytes(b'changed')
        with patch.object(kit,'check_hardware',side_effect=mutate_source), \
             patch.object(cmp70hx_boot,'detect',return_value='systemd'), patch.object(cmp70hx_boot,'enable'):
            kit.prepare_reboot(self.args)
        self.assertEqual((kit.PREFIX/'nvidia.ko').read_bytes(),module)

    def test_systemd_keeps_the_selected_python_for_kernel_and_mme(self):
        import cmp_mme_manage
        with patch.object(kit.sys,'executable','/usr/local/bin/python3.12'):
            kit.install(self.args)
            self.assertIn('ExecStart=/usr/local/bin/python3.12 ',(kit.SYSTEMD / kit.UNIT).read_text())
            with patch.object(kit,'kernel_ready'),patch.object(cmp_mme_manage.Manager,'inspect',
                    return_value={'files':[{'state':'patched'},{'state':'patched'}]}):
                kit.mme(argparse.Namespace(action='install',lib_dir='/usr/lib64'))
            self.assertIn('ExecStart=/usr/local/bin/python3.12 ',(kit.SYSTEMD / kit.MME_UNIT).read_text())

    def test_verify_failure_rolls_back_service_dependencies(self):
        self.failure = lambda args: args[:2] == ("systemd-analyze", "verify")
        with self.assertRaises(RuntimeError): kit.install(self.args)
        self.assertFalse(self.dropin().exists())
        self.assertFalse((kit.SYSTEMD / kit.UNIT).exists())
        self.assertFalse(kit.PREFIX.exists())
        self.failure = None
        kit.install(self.args)

    def interrupted(self, temporary=False):
        files = {kit.PREFIX / "cmp70hx.py": b"partial installation", self.dropin():
                 f"[Unit]\nRequires={kit.UNIT}\nAfter={kit.UNIT}\n".encode()}
        plan = dict(schema=1, id="123", files=[dict(path=str(p), sha256=kit.hashlib.sha256(b).hexdigest())
                                                for p, b in files.items()])
        kit.write_json(kit.STATE / "pending-install.json", plan)
        for p, data in files.items(): kit.atomic_write(p, data)
        if temporary: (kit.PREFIX / ".cmp_mme_manage.py.interrupted").write_bytes(b"partial")

    def test_hard_interruption_with_temporary_file_recovered_on_retry(self):
        self.interrupted(temporary=True)
        kit.install(self.args)
        self.assertFalse((kit.STATE / "pending-install.json").exists())
        self.assertTrue((kit.PREFIX / "install.json").exists())
        self.assertTrue(list((kit.STATE / "transactions/123").glob("remaining-*")))

    def test_changed_file_preserved_during_journal_recovery(self):
        self.interrupted()
        file = kit.PREFIX / "cmp70hx.py"
        file.write_bytes(b"operator edits")
        with self.assertRaisesRegex(RuntimeError, "preserves changed file"):
            kit.recover_install(None)
        self.assertEqual(file.read_bytes(), b"operator edits")
        self.assertTrue(self.dropin().exists())

    def test_recovery_rejects_journal_path_outside_owned_files(self):
        outside = self.root / "unrelated.txt"; outside.write_bytes(b"keep")
        kit.write_json(kit.STATE / "pending-install.json", dict(schema=1, id="42",
            files=[dict(path=str(outside), sha256=kit.sha(outside))]))
        with self.assertRaisesRegex(RuntimeError, "Invalid journal target"):
            kit.recover_install(None)
        self.assertEqual(outside.read_bytes(), b"keep")

    def test_old_partial_install_without_configuration_is_recoverable(self):
        kit.PREFIX.mkdir(); (kit.PREFIX / "cmp70hx.py").write_bytes(b"legacy partial")
        kit.atomic_write(self.dropin(), f"[Unit]\nRequires={kit.UNIT}\nAfter={kit.UNIT}\n".encode())
        kit.recover_install(None)
        self.assertFalse(kit.PREFIX.exists())
        self.assertFalse(self.dropin().exists())
        kit.install(self.args)

    def test_uninstall_after_kernel_update_does_not_query_old_fallback(self):
        kit.install(self.args)
        kit.DISABLED.touch()
        self.calls.clear()
        with patch.object(kit.platform, "release", return_value="new-kernel"), \
             patch.object(kit, "output", side_effect=AssertionError("Uninstall must not require old kernel/driver")):
            kit.uninstall(None)
        self.assertFalse(self.dropin().exists())
        self.assertFalse(kit.PREFIX.exists())
        self.assertFalse(kit.CONFIG.exists())

    def test_uninstall_even_when_config_is_missing(self):
        kit.install(self.args); kit.DISABLED.touch(); kit.CONFIG.unlink()
        kit.uninstall(None)
        self.assertFalse(self.dropin().exists())

    def standalone(self):
        self.c["services"] = []
        kit.write_json(self.build / "config.json", self.c)
        kit.write_json(self.build / "build.json", dict(module_sha256=kit.sha(self.build / "nvidia.ko"),
                                                      config_sha256=kit.sha(self.build / "config.json")))
        self.args.standalone = True

    def test_standalone_install_and_uninstall_never_call_systemd(self):
        self.standalone()
        kit.install(self.args)
        self.assertTrue(kit.standalone_installation())
        self.assertFalse((kit.SYSTEMD / kit.UNIT).exists())
        kit.DISABLED.touch()
        kit.uninstall(None)
        self.assertFalse(kit.PREFIX.exists())
        self.assertFalse(any(str(c[0]).startswith("system") for c in self.calls))

    def test_standalone_rejects_service_dependencies(self):
        self.args.standalone = True
        with self.assertRaisesRegex(RuntimeError, "empty services"):
            kit.install(self.args)
        self.assertFalse(kit.PREFIX.exists())

    def test_reboot_preparation_leaves_live_gpu_and_libraries_untouched(self):
        self.standalone(); self.args.lib_dir = None
        import cmp70hx_boot
        with patch.object(cmp70hx_boot, "detect", return_value="systemd"), \
             patch.object(cmp70hx_boot, "content", return_value=b"unit"), patch.object(cmp70hx_boot, "enable"), \
             patch.object(kit, "apply") as apply, patch.object(kit, "mme") as mme:
            kit.prepare_reboot(self.args)
            kit.prepare_reboot(self.args)
        apply.assert_not_called(); mme.assert_not_called()
        self.assertFalse(kit.DISABLED.exists())
        self.assertTrue(json.loads((kit.PREFIX / "install.json").read_text())["first_boot_pending"])
        self.assertEqual(json.loads((kit.STATE / "setup-result.json").read_text())["status"], "awaiting-reboot")
        self.assertFalse(any(c[0] in ("nvidia-smi", "insmod", "modprobe", "reboot", "fuser") for c in self.calls))

    def test_failed_reboot_setup_stays_disabled(self):
        self.standalone(); self.args.lib_dir = None
        import cmp70hx_boot
        with patch.object(cmp70hx_boot, "detect", return_value="systemd"), \
             patch.object(cmp70hx_boot, "content", return_value=b"unit"), \
             patch.object(cmp70hx_boot, "enable", side_effect=RuntimeError("failed enable")):
            with self.assertRaisesRegex(RuntimeError, "failed enable"): kit.prepare_reboot(self.args)
        self.assertTrue(kit.DISABLED.exists())
        self.assertFalse((kit.STATE / "setup-result.json").exists())

    def test_unknown_mme_library_refuses_before_installation(self):
        self.standalone(); self.args.lib_dir = "/usr/lib64"
        import cmp_mme_manage
        with patch.object(cmp_mme_manage.Manager, "inspect", return_value={"files": [{"state": "unknown"}, {"state": "original"}]}):
            with self.assertRaisesRegex(RuntimeError, "hashes unsupported"): kit.prepare_reboot(self.args)
        self.assertFalse(kit.PREFIX.exists())

    def test_standalone_failed_write_rolls_back_without_systemd(self):
        self.standalone()
        original = kit.atomic_write
        def fail(path, data):
            if path == kit.CONFIG: raise OSError("injected write failure")
            return original(path, data)
        with patch.object(kit, "atomic_write", side_effect=fail):
            with self.assertRaises(OSError): kit.install(self.args)
        self.assertFalse(kit.PREFIX.exists())
        self.assertFalse(any(str(c[0]).startswith("system") for c in self.calls))

    @unittest.skipUnless(os.name == "posix", "Linux library paths")
    def test_standalone_mme_does_not_create_service(self):
        self.standalone(); kit.install(self.args)
        import cmp_mme_manage
        with patch.object(kit, "kernel_ready"), patch.object(cmp_mme_manage.Manager, "inspect",
                return_value={"files": [{"state": "patched"}, {"state": "patched"}]}):
            kit.mme(argparse.Namespace(action="install", lib_dir=str(self.root / "libraries")))
        self.assertFalse((kit.SYSTEMD / kit.MME_UNIT).exists())
        self.assertTrue(any("apply" in c and "--lib-dir" in c for c in self.calls))
        self.assertFalse(any(str(c[0]).startswith("system") for c in self.calls))
        data = json.loads((kit.PREFIX / "install.json").read_text())
        data.update(mme_enabled=False, mme_state="restored")
        kit.write_json(kit.PREFIX / "install.json", data)
        with patch.object(kit, "kernel_ready"), patch.object(cmp_mme_manage.Manager, "inspect",
                return_value={"files": [{"state": "patched"}, {"state": "patched"}]}):
            kit.mme(argparse.Namespace(action="install", lib_dir=str(self.root / "libraries")))
        self.assertTrue(json.loads((kit.PREFIX / "install.json").read_text())["mme_enabled"])

    def test_standalone_readiness_uses_current_receipt_and_loaded_module(self):
        self.standalone(); kit.install(self.args)
        kit.write_json(kit.READY, dict(status="success", boot_id="current", config_sha256=kit.sha(kit.CONFIG),
                                     module_sha256=kit.sha(kit.PREFIX / "nvidia.ko")))
        with patch.object(kit, "boot_id", return_value="current"), patch.object(kit, "loaded_policy"), \
             patch.object(kit, "loaded_source", return_value="source"), patch.object(kit, "output", return_value="source"):
            kit.service_ready()
            with patch.object(kit, "boot_id", return_value="another-boot"):
                with self.assertRaisesRegex(RuntimeError, "current-boot"):
                    kit.service_ready()
        self.assertFalse(any(str(c[0]).startswith("system") for c in self.calls))

    def test_uninstall_preserves_unrelated_dropins(self):
        kit.install(self.args); kit.DISABLED.touch()
        extra = self.dropin().with_name("10-local.conf")
        extra.write_text("[Service]\nEnvironment=EXAMPLE=1\n")
        kit.uninstall(None)
        self.assertTrue(extra.exists())
        self.assertTrue(list(kit.STATE.glob("detached-services-*/*90-cmp70hx.conf")))

    def test_unknown_libraries_do_not_block_desktop_detachment(self):
        kit.install(self.args); kit.DISABLED.touch()
        path = kit.PREFIX / "install.json"
        data = json.loads(path.read_text()); data.update(mme=True, lib_dir="/usr/lib64")
        kit.write_json(path, data)
        self.failure = lambda args: args[0] == kit.sys.executable
        kit.uninstall(None)
        self.assertFalse(self.dropin().exists())
        report = json.loads(next(kit.STATE.glob("uninstalled-*/removal.json")).read_text())
        self.assertTrue(report["integration_removed"])
        self.assertIn("MME restore incomplete", report["warnings"][0])

    def test_uninstall_refuses_loaded_modified_module(self):
        kit.install(self.args); kit.DISABLED.touch()
        with patch.object(kit, "ensure_inactive_module", side_effect=RuntimeError("module loaded")):
            with self.assertRaises(RuntimeError): kit.uninstall(None)
        self.assertTrue(self.dropin().exists())

    @unittest.skipUnless(os.name == "posix", "Linux library paths")
    def test_mme_failed_start_can_be_retried(self):
        kit.install(self.args)
        import cmp_mme_manage
        folder = self.root / "libraries"; folder.mkdir()
        args = argparse.Namespace(action="install", lib_dir=str(folder))
        with patch.object(kit, "kernel_ready"), patch.object(cmp_mme_manage.Manager, "inspect",
                return_value={"files": [{"state": "patched"}, {"state": "patched"}]}):
            self.failure = lambda command: command[:2] == ("systemctl", "restart")
            with self.assertRaises(RuntimeError): kit.mme(args)
            self.assertEqual(json.loads((kit.PREFIX / "install.json").read_text())["mme_state"], "pending")
            self.failure = None
            kit.mme(args)
        self.assertEqual(json.loads((kit.PREFIX / "install.json").read_text())["mme_state"], "complete")

    @unittest.skipUnless(os.name == "posix", "Linux library paths")
    def test_mme_unit_write_failure_can_be_retried(self):
        kit.install(self.args)
        import cmp_mme_manage
        args = argparse.Namespace(action="install", lib_dir=str(self.root / "libraries"))
        original = kit.atomic_write
        def fail(path, data):
            if path == kit.SYSTEMD / kit.MME_UNIT: raise OSError("injected unit write failure")
            return original(path, data)
        with patch.object(kit, "kernel_ready"), patch.object(cmp_mme_manage.Manager, "inspect",
                return_value={"files": [{"state": "patched"}, {"state": "patched"}]}):
            with patch.object(kit, "atomic_write", side_effect=fail):
                with self.assertRaises(OSError): kit.mme(args)
            self.assertFalse((kit.SYSTEMD / kit.MME_UNIT).exists())
            kit.mme(args)
        self.assertEqual(json.loads((kit.PREFIX / "install.json").read_text())["mme_state"], "complete")

    @unittest.skipUnless(os.name == "posix", "Linux library paths")
    def test_mme_rejects_systemd_specifier_in_path(self):
        kit.install(self.args)
        with patch.object(kit, "kernel_ready"):
            with self.assertRaisesRegex(RuntimeError, "systemd specifiers"):
                kit.mme(argparse.Namespace(action="install", lib_dir=str(self.root / "%n")))
        self.assertFalse((kit.SYSTEMD / kit.MME_UNIT).exists())

    @unittest.skipUnless(os.name == "posix", "Linux fcntl lock")
    def test_administrative_operations_share_exclusive_lock(self):
        with kit.admin_lock():
            with self.assertRaises(BlockingIOError):
                with kit.admin_lock(): pass


class AutoSelection(unittest.TestCase):
    def test_all_skips_unsupported_but_explicit_selection_refuses(self):
        supported = dict(bdf="0000:01:00.0", ids=dict(kit.IDS), width=4, supported=True)
        unsupported = copy.deepcopy(supported)
        unsupported.update(bdf="0000:02:00.0", supported=False)
        unsupported["ids"]["subsystem_device"] = "0x0000"
        with tempfile.TemporaryDirectory() as tmp, patch.object(kit, "inventory", return_value=[supported, unsupported]), \
                patch.object(kit, "output", return_value="/lib/modules/kernel/nvidia.ko"):
            args = argparse.Namespace(cards="all", pcie="", gfx="all", services="", output=str(Path(tmp)/"host.json"))
            kit.configure(args)
            data = json.loads(Path(args.output).read_text())
            self.assertEqual([c["bdf"] for c in data["cards"]], [supported["bdf"]])
            args.cards = unsupported["bdf"]; args.output = str(Path(tmp)/"refused.json")
            with self.assertRaisesRegex(RuntimeError, "Unsupported"):
                kit.configure(args)

    def test_all_with_no_supported_cards_has_clear_error(self):
        with patch.object(kit, "inventory", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "No supported"):
                kit.configure(argparse.Namespace(cards="all"))


if __name__ == "__main__":
    unittest.main()
