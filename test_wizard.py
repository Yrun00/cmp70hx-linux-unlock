"""Interactive selection and standalone orchestration without physical GPU access."""
import argparse
import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

import cmp70hx as kit
import cmp70hx_wizard as wizard
from test_kit import config


class Selection(unittest.TestCase):
    def test_numbers_reject_invalid_or_duplicate_indices(self):
        for value in ("0", "3", "1,1", "-1", "1;reboot", "", "none"):
            with self.subTest(value=value), self.assertRaises(RuntimeError): wizard.numbers(value, 2)
        self.assertEqual(wizard.numbers("2,1", 2), [1, 0])
        self.assertEqual(wizard.numbers("none", 2, True), [])

    def test_subset_selection_keeps_correct_physical_card(self):
        cards = [dict(bdf="0000:01:00.0", width=4, supported=True),
                 dict(bdf="0000:02:00.0", width=1, supported=True),
                 dict(bdf="0000:03:00.0", width=4, supported=False, ids={})]
        with patch("builtins.input", side_effect=["2", "1", "none"]):
            chosen, gfx, pcie = wizard.choose_cards(cards)
        self.assertEqual([c["bdf"] for c in chosen], ["0000:02:00.0"])
        self.assertEqual(gfx, ["0000:02:00.0"])
        self.assertEqual(pcie, [])

    def test_default_entry_starts_wizard(self):
        with patch.object(wizard, "main") as start:
            kit.main([])
        start.assert_called_once()

    def test_prerequisites_do_not_require_application_services(self):
        with patch.object(wizard.sys, "platform", "linux"), patch.object(kit.platform, "machine", return_value="x86_64"), \
             patch.object(wizard.os, "geteuid", return_value=1000, create=True), \
             patch.object(wizard.boot_setup, "detect", return_value="openrc"), \
             patch.object(wizard.sys, "executable", "/usr/bin/python3"), \
             patch.object(wizard.shutil, "which", return_value="/tool") as which, \
             patch.object(kit, "output", side_effect=[kit.VERSION, "Dual MIT/GPL"]), \
             patch.object(Path, "is_file", return_value=True), patch.object(Path, "exists", return_value=False):
            wizard.check_host()
        self.assertFalse(any("system" in c.args[0] or c.args[0] == "loginctl" for c in which.call_args_list))


class Finish(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.c = config(); self.c["services"] = []
        (self.folder / "build").mkdir()
        kit.write_json(self.folder / "build/config.json", self.c)
        (self.folder / "build/nvidia.ko").write_bytes(b"module")
        kit.write_json(self.folder / "wizard.json", dict(schema=1, phase="built",
            config_sha256=kit.sha(self.folder / "build/config.json"), module_sha256=kit.sha(self.folder / "build/nvidia.ko")))
        self.stack.enter_context(patch.object(kit.platform, "release", return_value=self.c["kernel"]))
        self.stack.enter_context(patch.object(kit, "check_hardware"))
        self.stack.enter_context(patch.object(kit, "DISABLED", self.folder / "disabled"))
        self.stack.enter_context(patch.object(wizard, "library_directories", return_value=[]))
        self.stack.enter_context(patch.object(wizard.boot_setup, "detect", return_value="systemd"))
        self.calls = []
        self.stack.enter_context(patch.object(wizard, "root_run", side_effect=lambda *a, **kw: self.calls.append(a)))

    def test_cancel_never_changes_system(self):
        with patch("builtins.input", return_value=""): wizard.finish(self.folder)
        self.assertEqual(self.calls, [])

    def test_install_standalone_without_app_or_service_actions(self):
        kit.DISABLED.touch()
        with patch("builtins.input", side_effect=["skip", "INSTALL"]): wizard.finish(self.folder)
        self.assertTrue(any("prepare-reboot" in c for c in self.calls))
        self.assertFalse(any("systemctl" in c or "loginctl" in c for c in self.calls))
        self.assertFalse(any("apply" in c or "reboot" in c for c in self.calls))
        self.assertEqual(json.loads((self.folder / "wizard.json").read_text())["phase"], "awaiting-reboot")

    def test_preparation_failure_does_not_reboot(self):
        def fail(*args, **kwargs):
            self.calls.append(args)
            if "prepare-reboot" in args: raise RuntimeError("preparation failed")
        with patch.object(wizard, "root_run", side_effect=fail), patch("builtins.input", side_effect=["skip", "REBOOT"]):
            with self.assertRaisesRegex(RuntimeError, "preparation failed"): wizard.finish(self.folder)
        self.assertFalse(any("reboot" in c for c in self.calls))
        self.assertEqual(json.loads((self.folder / "wizard.json").read_text())["phase"], "preparing-reboot")

    def test_changed_build_is_refused_before_sudo(self):
        (self.folder / "build/nvidia.ko").write_bytes(b"different")
        with self.assertRaisesRegex(RuntimeError, "changed"): wizard.finish(self.folder)
        self.assertEqual(self.calls, [])

    def test_explicit_reboot_runs_only_after_staging(self):
        with patch("builtins.input", side_effect=["/usr/lib64", "REBOOT"]): wizard.finish(self.folder)
        self.assertEqual(self.calls[-1], ("reboot",))
        prepared = next(c for c in self.calls if "prepare-reboot" in c)
        self.assertIn("--lib-dir", prepared)
        self.assertIn("/usr/lib64", prepared)

    def test_inhibited_reboot_keeps_prepared_installation_and_explains_manual_restart(self):
        def command(*args, **kwargs):
            self.calls.append(args)
            if args == ("reboot",): raise RuntimeError("session inhibited")
        with patch.object(wizard,"root_run",side_effect=command), \
             patch("builtins.input",side_effect=["skip","REBOOT"]), contextlib.redirect_stdout(io.StringIO()) as screen:
            wizard.finish(self.folder)
        self.assertEqual(json.loads((self.folder / "wizard.json").read_text())["phase"], "awaiting-reboot")
        self.assertIn("restart through the desktop menu",screen.getvalue())
        self.assertNotIn("Stopped at:",screen.getvalue())
        self.assertEqual(self.calls[-1],("reboot",))


class BuildProgress(unittest.TestCase):
    def test_live_output_arrives_before_child_finishes_and_is_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); done = root / "done"
            observed = []
            class Terminal(io.StringIO):
                def write(self, text):
                    if "compiler-start" in text: observed.append(not done.exists())
                    return super().write(text)
            script = "import sys,time,pathlib; print('compiler-start',flush=True); time.sleep(0.3); pathlib.Path(sys.argv[1]).touch(); print('compiler-end',flush=True)"
            with contextlib.redirect_stdout(Terminal()):
                wizard.stream_build([sys.executable, "-u", "-c", script, str(done)], root / "build.log")
            self.assertEqual(observed, [True])
            self.assertEqual((root / "build.log").read_text(), "compiler-start\ncompiler-end\n")

    def test_silent_build_reports_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as screen:
            wizard.stream_build([sys.executable, "-c", "import time; time.sleep(0.2)"], Path(directory)/"build.log", heartbeat=0.02)
            self.assertIn("Build running:", screen.getvalue())

    @unittest.skipUnless(sys.platform == "linux", "Linux process groups")
    def test_failed_build_preserves_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            log = Path(directory) / "build.log"
            with self.assertRaisesRegex(RuntimeError, "Build failed"):
                wizard.stream_build([sys.executable, "-c", "print('compiler error'); raise SystemExit(2)"], log)
            self.assertIn("compiler error", log.read_text())


@unittest.skipUnless(sys.platform == "linux", "Linux private installer staging")
class InstallerTransfer(unittest.TestCase):
    def exercise(self, corrupt=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); sources = root / "sources"; sources.mkdir()
            result = root / "result.json"
            code = '''import json, os
from pathlib import Path
def main(args):
    path = Path(__file__)
    Path(args[2]).write_text(json.dumps({"uid": path.stat().st_uid, "mode": path.parent.stat().st_mode & 511, "path": str(path)}))
'''
            for name in ("cmp70hx.py", "cmp70hx_boot.py", "cmp_mme_manage.py"):
                (sources / name).write_text(code if name == "cmp70hx.py" else "")
            def invoke(*args, input=None, **kwargs):
                self.assertEqual(args[1:3], ("-I", "-c"))
                self.assertNotIn(str(sources / "cmp70hx.py"), args)
                (sources / "cmp70hx.py").write_text("raise RuntimeError('replaced source was executed')")
                if corrupt: input += b"changed"
                return subprocess.run(args, input=input, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
            with patch.object(kit, "ROOT", sources), patch.object(wizard, "root_run", side_effect=invoke) as call:
                wizard.privileged_prepare(result, "skip")
            self.assertEqual(call.call_count, 1)
            if corrupt:
                self.assertFalse(result.exists())
            else:
                data = json.loads(result.read_text())
                self.assertEqual(data["mode"], 0o700)
                self.assertEqual(data["uid"], os.geteuid())
                self.assertFalse(Path(data["path"]).exists())

    def test_code_snapshot_survives_user_path_replacement(self):
        self.exercise()

    def test_transfer_hash_mismatch_does_not_execute_code(self):
        self.exercise(corrupt=True)


class ResumeUpgrade(unittest.TestCase):
    def test_completed_build_is_preserved_when_installer_is_updated(self):
        import package
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            wizard.copy_kit(folder / "kit")
            (folder / "kit/cmp70hx.py").write_text("old installer")
            (folder / "build").mkdir()
            module = folder / "build/nvidia.ko"; module.write_bytes(b"keep completed module")
            wizard.upgrade_setup(folder)
            self.assertEqual(module.read_bytes(), b"keep completed module")
            self.assertEqual((folder / "kit/cmp70hx.py").read_bytes(), (package.ROOT / "cmp70hx.py").read_bytes())
            old = next(folder.glob("kit-previous-*"))
            self.assertEqual((old / "cmp70hx.py").read_text(), "old installer")

    def test_different_driver_overlay_requires_new_build(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            wizard.copy_kit(folder / "kit")
            (folder / "kit/PROVENANCE.json").write_text("different overlay")
            with self.assertRaisesRegex(RuntimeError, "Source overlay changed"):
                wizard.upgrade_setup(folder)


if __name__ == "__main__":
    unittest.main()
