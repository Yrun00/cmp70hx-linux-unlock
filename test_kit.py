"""CPU-only tests: no subprocess, driver initialization or BAR access."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import os
import argparse
import contextlib
import subprocess
from unittest.mock import patch

import cmp70hx as kit
import cmp_mme_manage as mme


def config(bdfs=("0000:01:00.0",), pcie=True, gfx=True):
    return dict(schema=1, driver=kit.VERSION, kernel="test-kernel",
                stock_module="/lib/modules/test-kernel/updates/nvidia.ko", services=[],
                cards=[dict(bdf=bdf, ids=dict(kit.IDS), width=4, compute=True, pcie=pcie, gfx=gfx)
                       for bdf in bdfs])


class Config(unittest.TestCase):
    def test_single_and_multiple_targets(self):
        for bdfs in [("0000:01:00.0",), ("0001:41:02.0", "0000:03:00.0")]:
            self.assertEqual(len(kit.validate(config(bdfs))["cards"]), len(bdfs))

    def test_unknown_subsystem_duplicate_function_and_injection(self):
        variants = []
        c = config(); c["cards"][0]["ids"]["subsystem_device"] = "0xffff"; variants.append(c)
        c = config(("0000:01:00.0", "0000:01:00.0")); variants.append(c)
        c = config(("0000:01:00.1",)); variants.append(c)
        c = config(); c["kernel"] = "foo;touch /tmp/x"; variants.append(c)
        c = config(); c["services"] = ["sshd.service"]; variants.append(c)
        c = config(); c["services"] = ["--user.service"]; variants.append(c)
        c = config(); c["services"] = ["foo.service\nExecStart=x"]; variants.append(c)
        c = config(); c["cards"][0]["width"] = 16; variants.append(c)
        c = config(); c["stock_module"] = "/tmp/nvidia.ko"; variants.append(c)
        for c in variants:
            with self.subTest(c=c), self.assertRaises(RuntimeError):
                kit.validate(c)

    def test_compute_only_avoids_protected_gfx_pcie_stages(self):
        self.assertEqual(kit.stages(config(pcie=False, gfx=False)),
                         [(1, None, False), (2, None, False), (5, None, False)])

    def test_complete_order_and_gfx_without_pcie(self):
        self.assertEqual(kit.stages(config()), [
            (1, None, False), (4, 1, False), (4, 2, False),
            (4, 3, False), (2, None, False), (5, None, True)])
        self.assertEqual(kit.stages(config(pcie=False)),
                         [(1, None, False), (4, 3, False), (2, None, False), (5, None, True)])

    def test_discovery_is_metadata_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "fixture"
            p.mkdir()
            for key, value in kit.IDS.items():
                (p / key).write_text(value)
            (p / "current_link_width").write_text("4")
            class Device:
                name = "0001:41:02.0"
                def __truediv__(self, name):
                    return p / name
            class Sysfs:
                def glob(self, pattern):
                    return [Device()]
            with patch.object(kit, "run", side_effect=AssertionError("No process allowed")):
                self.assertEqual(kit.inventory(Sysfs())[0]["bdf"], Device.name)


class Generation(unittest.TestCase):
    def test_overlay_integrity_and_host_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c = config(("0001:41:02.0", "0000:03:00.0"))
            c["cards"][1].update(pcie=False, gfx=False, width=1)
            kit.generate_sources(c, root)
            nv = (root / "kernel-open/nvidia/nv.c").read_text()
            header = (root / "src/nvidia/inc/kernel/gpu/gsp/cmp70_port.h").read_text()
            for text in (nv, header):
                self.assertNotIn("0000:10:00.0", text)
                self.assertNotIn("0000:23:00.0", text)
            self.assertIn("p->devfn == 0x10", nv)  # device 2, function 0
            self.assertIn("gpuGetDomain(pGpu) == 0x0001", header)
            pcie_target = nv[nv.index("static NvBool nv_cmp70_pcie_target"):nv.index("static NvBool nv_cmp70_gfx_target")]
            self.assertNotIn("0x03", pcie_target)
            # Remaining four files preserve the historical protected payload byte-for-byte.
            data = json.loads((kit.ROOT / "PROVENANCE.json").read_text())
            changed = {"kernel-open/nvidia/nv.c", "src/nvidia/inc/kernel/gpu/gsp/cmp70_port.h"}
            for entry in data["files"]:
                rel = Path(entry["file"]).relative_to("overlay")
                if rel.as_posix() not in changed:
                    self.assertEqual(kit.sha(root / rel), entry["sha256"])

    def test_compute_only_and_variable_width(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = config(pcie=False, gfx=False)
            kit.generate_sources(c, Path(tmp))
            nv = (Path(tmp) / "kernel-open/nvidia/nv.c").read_text()
            self.assertIn("nv_cmp70_pcie_target(struct pci_dev *p) { return p && 0; }", nv)
            self.assertIn("nv_cmp70_gfx_target(struct pci_dev *p) { return p && 0; }", nv)
        with tempfile.TemporaryDirectory() as tmp:
            c = config(); c["cards"][0]["width"] = 1
            kit.generate_sources(c, Path(tmp))
            nv = (Path(tmp) / "kernel-open/nvidia/nv.c").read_text()
            self.assertIn("return 1;", nv)
            self.assertIn("PCI_EXP_LNKSTA_NLW_SHIFT) == nv_cmp70_expected_width(gpu)", nv)

    def test_source_edit_mismatch_refuses(self):
        with self.assertRaises(RuntimeError):
            kit.replace_once("abc abc", "abc", "x")

    def test_driver_archive_hash_rejects_before_unpack(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "driver.tar.xz"
            archive.write_bytes(b"wrong version")
            with self.assertRaises(RuntimeError):
                kit.unpack(archive, Path(tmp) / "out")
            self.assertFalse((Path(tmp) / "out").exists())


class Readback(unittest.TestCase):
    def registers(self, gfx=True):
        return {0x823804: 0xffffffff, 0x82381c: 0x88888888, 0x823820: 8,
                0x823b04: 0xffffffff, 0x823830: 4 if gfx else 3, 0x823834: 0 if gfx else 3}

    def test_compute_and_gfx_and_riser_exclusion(self):
        c = config(("0000:01:00.0", "0000:02:00.0"), pcie=False)
        c["cards"][1]["gfx"] = False
        baseline = {x["bdf"]: self.registers(False) for x in c["cards"]}
        with patch.object(kit, "read_registers", side_effect=lambda card: self.registers(card["gfx"])):
            self.assertEqual(len(kit.verify(c, baseline)), 2)
        with patch.object(kit, "read_registers", return_value=self.registers(True)):
            with self.assertRaisesRegex(RuntimeError, "Unselected"):
                kit.verify(c, baseline)

    def test_wrong_compute_or_gfx_is_failure(self):
        for address, value in [(0x82381c, 0), (0x823834, 3), (0x823b04, 0xffffff8f)]:
            registers = self.registers(); registers[address] = value
            with patch.object(kit, "read_registers", return_value=registers):
                with self.assertRaises(RuntimeError):
                    kit.verify(config(pcie=False))

    def test_wait_guard_prevents_gpu_probe(self):
        with patch.object(kit, "output", return_value="activating"):
            with self.assertRaisesRegex(RuntimeError, "no GPU probes"):
                kit.service_ready()

    def test_loaded_mode_must_match_final_policy(self):
        with patch.object(kit.Path, "read_text", side_effect=["5", "1"]):
            kit.loaded_policy(config())
        with patch.object(kit.Path, "read_text", return_value="2"):
            with self.assertRaisesRegex(RuntimeError, "Final driver mode"):
                kit.loaded_policy(config())
        with patch.object(kit.Path, "read_text", side_effect=["5", "0"]):
            with self.assertRaisesRegex(RuntimeError, "GFX policy"):
                kit.loaded_policy(config())

    def test_unload_failure_stops_before_next_module(self):
        calls = []
        def fail(*args, **kwargs):
            calls.append(args)
            raise RuntimeError("module in use")
        with patch.object(kit.Path, "exists", return_value=True), patch.object(kit, "run", side_effect=fail):
            with self.assertRaises(RuntimeError):
                kit.unload()
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--force", calls[0])


class MME(unittest.TestCase):
    def test_fixed_hash_allowlist_and_two_offsets(self):
        self.assertEqual(len(mme.ENTRIES), 2)
        for item in mme.ENTRIES:
            self.assertEqual(len(item["offsets"]), 2)
            self.assertEqual(len(item["original"]), 64)
            self.assertNotEqual(item["original"], item["patched"])

    def test_custom_library_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = mme.Manager(Path(tmp), lib_dir="/usr/lib64")
            self.assertEqual(m.libs, Path(tmp) / "usr/lib64")
            self.assertTrue(all(f["state"] == "unknown" for f in m.inspect()["files"]))
        with self.assertRaises(ValueError):
            mme.Manager(lib_dir="/usr/../tmp")


@unittest.skipUnless(os.name == "posix", "Linux fcntl lock; every GPU/command call is mocked")
class Lifecycle(unittest.TestCase):
    def exercise(self, fail=False, already_open=False, persistence=False, recovery_failure=False, terminate=False):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            p = Path(tmp); prefix = p / "install"; prefix.mkdir()
            c = config(); c["services"] = ["worker.service"]
            cfg = p / "config.json"; kit.write_json(cfg, c)
            (prefix / "nvidia.ko").write_bytes(b"test module")
            kit.write_json(prefix / "build.json", dict(config_sha256=kit.sha(cfg),
                                                      module_sha256=kit.sha(prefix / "nvidia.ko")))
            for name, value in dict(PREFIX=prefix, CONFIG=cfg, STATE=p / "state",
                                    READY=p / "ready.json", DISABLED=p / "disabled").items():
                stack.enter_context(patch.object(kit, name, value))
            for name in ("root_required", "check_hardware"):
                stack.enter_context(patch.object(kit, name))
            stack.enter_context(patch.object(kit, "module_loaded", return_value=persistence))
            stack.enter_context(patch.object(kit, "nvidia_device_nodes", return_value=[]))
            stack.enter_context(patch.object(kit, "loaded_source", return_value="fixture"))
            stack.enter_context(patch.object(kit, "loaded_policy"))
            stack.enter_context(patch.object(kit.platform, "release", return_value=c["kernel"]))
            stack.enter_context(patch.object(kit, "boot_id", return_value="test-boot"))
            def output(*args):
                if "-n" in args: return c["stock_module"]
                if "vermagic" in args: return c["kernel"] + " SMP"
                if "srcversion" in args: return "fixture"
                if "license" in args: return "Dual MIT/GPL"
                return kit.VERSION
            stack.enter_context(patch.object(kit, "output", side_effect=output))
            calls = []
            gpu='GPU-11111111-1111-1111-1111-111111111111'
            current_mode=['Enabled']
            def command(*args, **kwargs):
                calls.append(args)
                if args[:2]==('modprobe','nvidia_drm') and recovery_failure:
                    raise RuntimeError('injected stock recovery failure')
                if '--query-gpu=uuid,persistence_mode' in args:
                    return subprocess.CompletedProcess(args,0,gpu+', '+current_mode[0]+'\n')
                if '-pm' in args:
                    before=json.loads((kit.STATE/'last-run.json').read_text())
                    self.assertEqual(before['persistence_before'],[[gpu,'Enabled']])
                    current_mode[0]='Enabled' if args[-1]=='1' else 'Disabled'
                return subprocess.CompletedProcess(args, 0, "")
            stack.enter_context(patch.object(kit, "run", side_effect=command))
            stages = []
            def initialize(module, mode, index, gfx):
                stages.append((mode, index, gfx))
                if terminate and mode == 2: raise kit.ActivationTerminated("injected SIGTERM")
                if fail and mode == 2: raise RuntimeError("injected boot failure")
            stack.enter_context(patch.object(kit, "initialize", side_effect=initialize))
            unload = stack.enter_context(patch.object(kit, "unload"))
            registers = {a: (0xffffffff if already_open else 0xffffff8f)
                         for a in (0x88fe8, 0x823800, 0x823b04)}
            stack.enter_context(patch.object(kit, "read_registers", return_value=registers))
            stack.enter_context(patch.object(kit, "verify", return_value={"fixture": "verified"}))
            if terminate:
                with self.assertRaises(kit.ActivationTerminated):
                    kit.apply(argparse.Namespace(ack_shared_driver=True))
                self.assertFalse(kit.READY.exists())
                self.assertNotIn(("modprobe", "nvidia_drm", "modeset=1"), calls)
                self.assertNotIn(("systemctl", "start", "--no-block", "worker.service"), calls)
                self.assertEqual(unload.call_count, len(stages))
                receipt = json.loads((kit.STATE / "last-run.json").read_text())
                self.assertEqual(receipt["status"], "failed")
                self.assertIn("withheld after termination", receipt["recovery"])
            elif fail:
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    kit.apply(argparse.Namespace(ack_shared_driver=True))
                self.assertFalse((p / "ready.json").exists())
                self.assertIn(("modprobe", "nvidia_drm", "modeset=1"), calls)
                self.assertNotIn(("systemctl", "start", "--no-block", "worker.service"), calls)
                receipt = json.loads((p / "state/last-run.json").read_text())
                self.assertEqual(receipt["status"], "failed")
                self.assertIn("FAILED" if recovery_failure else "stock module loaded", receipt["recovery"])
            else:
                kit.apply(argparse.Namespace(ack_shared_driver=True))
                self.assertEqual(json.loads((p / "ready.json").read_text())["boot_id"], "test-boot")
                self.assertIn(("systemctl", "start", "--no-block", "worker.service"), calls)
                expected = kit.stages(c)
                if already_open: expected = [s for s in expected if s[0] != 4]
                self.assertEqual(stages, expected)
                # A second call must verify and return without cycling modules.
                count = unload.call_count
                kit.apply(argparse.Namespace(ack_shared_driver=True))
                self.assertEqual(unload.call_count, count)
            if persistence:
                receipt=json.loads((kit.STATE/'last-run.json').read_text())
                self.assertEqual(receipt['persistence_restore']['status'],'withheld' if recovery_failure or terminate else 'restored')
                self.assertEqual(current_mode[0],'Disabled' if recovery_failure or terminate else 'Enabled')
                if recovery_failure or terminate:
                    self.assertEqual(sum('--query-gpu=uuid,persistence_mode' in c for c in calls),1)

    def test_success_and_same_boot_idempotence(self):
        self.exercise()

    def test_failure_recovers_stock_and_holds_consumers(self):
        self.exercise(fail=True)

    def test_already_open_gates_are_skipped(self):
        self.exercise(already_open=True)

    def test_persistence_restored_after_success(self):
        self.exercise(persistence=True)

    def test_persistence_restored_after_responsive_stock_recovery(self):
        self.exercise(fail=True,persistence=True)

    def test_failed_driver_recovery_withholds_further_gpu_calls(self):
        self.exercise(fail=True,persistence=True,recovery_failure=True)

    def test_termination_does_not_start_recovery_or_gpu_clients(self):
        self.exercise(persistence=True,terminate=True)


if __name__ == "__main__":
    unittest.main()
