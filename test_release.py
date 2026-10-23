"""Public release regressions; no GPU, init or privileged operations."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import stat
import tempfile
import unittest
from unittest.mock import patch

import cmp70hx as kit
import cmp_mme_manage as mme
import package


class Readiness(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.stack=contextlib.ExitStack(); self.addCleanup(self.stack.close)
        for name,path in dict(PREFIX=self.root/'kit',CONFIG=self.root/'config.json',
                              STATE=self.root/'state',READY=self.root/'ready.json',DISABLED=self.root/'disabled').items():
            self.stack.enter_context(patch.object(kit,name,path))
        self.stack.enter_context(patch.object(kit,'kernel_ready'))
        self.stack.enter_context(patch.object(kit,'boot_id',return_value='current'))
        kit.write_json(kit.CONFIG,{})
        kit.write_json(kit.PREFIX/'install.json',dict(integration='standalone',boot_backend='systemd',mme=True,lib_dir='/usr/lib64'))
        (kit.PREFIX/'nvidia.ko').write_bytes(b'module')
        kit.write_json(kit.READY,dict(started=42))
        self.receipt=dict(status='success',boot_id='current',kernel_started=42,
                          config_sha256=kit.sha(kit.CONFIG),module_sha256=kit.sha(kit.PREFIX/'nvidia.ko'))
        kit.write_json(kit.STATE/'boot-result.json',self.receipt)
        self.inspect=self.stack.enter_context(patch.object(mme.Manager,'inspect',return_value={
            'files':[{'state':'patched'},{'state':'patched'}]}))

    def test_completed_boot_and_both_mme_hashes_required(self):
        kit.service_ready()
        self.inspect.assert_called_once()

    def test_partial_or_changed_mme_fails_overall_readiness(self):
        for states in [('patched','original'),('patched','unknown'),('patched',)]:
            self.inspect.return_value={'files':[{'state':s} for s in states]}
            with self.assertRaisesRegex(RuntimeError,'MME libraries differ'): kit.service_ready()

    def test_incomplete_stale_or_different_boot_is_refused(self):
        for changes in [dict(status='failed'),dict(status='running'),dict(boot_id='old'),
                        dict(config_sha256='changed'),dict(module_sha256='changed'),dict(kernel_started=41)]:
            kit.write_json(kit.STATE/'boot-result.json',dict(self.receipt,**changes))
            with self.assertRaises(RuntimeError): kit.service_ready()

    def test_disabled_activation_is_refused(self):
        kit.DISABLED.touch()
        with self.assertRaisesRegex(RuntimeError,'disabled'): kit.service_ready()


class DeviceNodes(unittest.TestCase):
    def test_nvidia_drm_uvm_modeset_and_caps_but_no_amd(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); dev=root/'dev'; drm=root/'drm'
            names=['nvidia0','nvidiactl','nvidia-uvm','nvidia-uvm-tools','nvidia-modeset',
                   'nvidia-caps/nvidia-cap1','dri/card1','dri/renderD129']
            for name in names+['dri/card0','dri/renderD128']:
                path=dev/name; path.parent.mkdir(parents=True,exist_ok=True); path.touch()
            for node,vendor in [('card1','0x10de'),('renderD129','0x10de'),('card0','0x1002'),('renderD128','0x1002')]:
                path=drm/node/'device/vendor'; path.parent.mkdir(parents=True); path.write_text(vendor)
            self.assertEqual(set(kit.nvidia_device_nodes(dev,drm)),{dev/name for name in names})


@unittest.skipUnless(os.name=='posix','Linux permissions')
class PrivateDiagnostics(unittest.TestCase):
    def test_existing_state_and_boot_log_permissions_are_restricted(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(kit,'STATE',Path(directory)/'state'):
            kit.STATE.mkdir(mode=0o755)
            log=kit.STATE/'boot.log'; log.write_text('old\n'); log.chmod(0o644)
            with kit.private_log(log) as handle: handle.write('new\n')
            kit.write_json(kit.STATE/'last-run.json',{'private':'GPU identifiers'})
            self.assertEqual(stat.S_IMODE(kit.STATE.stat().st_mode),0o700)
            self.assertEqual(stat.S_IMODE(log.stat().st_mode),0o600)
            self.assertEqual(stat.S_IMODE((kit.STATE/'last-run.json').stat().st_mode),0o600)
            self.assertEqual(log.read_text(),'old\nnew\n')

    def test_symlinked_state_and_log_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); outside=root/'outside'; outside.mkdir()
            with patch.object(kit,'STATE',root/'state'):
                kit.STATE.symlink_to(outside)
                with self.assertRaises(RuntimeError): kit.write_json(kit.STATE/'result.json',{})
                kit.STATE.unlink(); kit.STATE.mkdir()
                target=outside/'log'; target.write_text('keep')
                (kit.STATE/'boot.log').symlink_to(target)
                with self.assertRaises(OSError):
                    with kit.private_log(kit.STATE/'boot.log'):pass
                self.assertEqual(target.read_text(),'keep')


class Persistence(unittest.TestCase):
    def test_changed_gpu_inventory_prevents_restore(self):
        report={}
        with patch.object(kit,'persistence_snapshot',return_value=[('other','Disabled')]),patch.object(kit,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'inventory changed'):
                kit.restore_persistence([('original','Enabled')],report)
        run.assert_not_called()
        self.assertEqual(report['status'],'failed')

    def test_restore_readback_mismatch_is_reported(self):
        report={}
        with patch.object(kit,'persistence_snapshot',return_value=[('same','Disabled')]),patch.object(kit,'run'):
            with self.assertRaisesRegex(RuntimeError,'readback differs'):
                kit.restore_persistence([('same','Enabled')],report)
        self.assertFalse(report['restored'])
        self.assertEqual(report['observed'],{'same':'Disabled'})


class Packaging(unittest.TestCase):
    def test_rebuild_ignores_source_timestamps_and_permissions(self):
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            for path in package.source_files():
                target = source / path.relative_to(package.ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
            with patch.object(package, 'ROOT', source), contextlib.redirect_stdout(io.StringIO()):
                with patch.object(sys, 'argv', ['package.py', str(root / 'first')]):
                    package.main()
                for path in package.source_files():
                    os.utime(path, (1234567890, 1234567890))
                    path.chmod(0o600)
                with patch.object(sys, 'argv', ['package.py', str(root / 'second')]):
                    package.main()
            for path in (root / 'first').iterdir():
                self.assertEqual(path.read_bytes(), (root / 'second' / path.name).read_bytes())
            with zipfile.ZipFile(root / 'first' / (package.NAME + '.zip')) as archive:
                for path in source.rglob('*'):
                    if path.is_file():
                        self.assertEqual(path.read_bytes(), archive.read(package.NAME + '/' + path.relative_to(source).as_posix()))

    def test_crlf_source_is_refused_before_creating_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.txt'
            source.write_bytes(b'line\r\n')
            with patch.object(package, 'ROOT', root), patch.object(package, 'source_files', return_value=[source]), \
                    patch.object(sys, 'argv', ['package.py', str(root / 'dist')]):
                with self.assertRaisesRegex(SystemExit, 'LF line endings'):
                    package.main()
            self.assertEqual(list((root / 'dist').iterdir()), [])

    def test_combined_source_license_tags_match_provenance(self):
        import hashlib
        import re
        entries = json.loads((kit.ROOT / 'PROVENANCE.json').read_text())['files']
        combined = [e for e in entries if e['license'] == 'GPL-2.0-only']
        self.assertEqual(len(combined), 3)
        for entry in combined:
            source = (kit.ROOT / entry['file']).read_bytes()
            self.assertEqual(hashlib.sha256(source).hexdigest(), entry['sha256'])
            text = source.decode()
            self.assertEqual(re.findall(r'SPDX-License-Identifier: ([^\r\n]+)', text), ['GPL-2.0-only'])
            self.assertIn('NVIDIA CORPORATION & AFFILIATES', text)
            self.assertIn('Permission is hereby granted, free of charge', text)
            self.assertIn('THE SOFTWARE IS PROVIDED "AS IS"', text)

    def test_only_explicit_public_files_are_packaged(self):
        with tempfile.TemporaryDirectory() as directory:
            dest=Path(directory)
            for path in package.source_files():
                target=dest/path.relative_to(package.ROOT)
                target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(path,target)
            for name in ['docs/private.env','docs/notes.md','overlay/debug.log','.github/token.key']:
                path=dest/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_text('private')
            with patch.object(package,'ROOT',dest):
                files={p.relative_to(dest).as_posix() for p in package.source_files()}
            self.assertIn('LICENSES/GPL-2.0-only.txt',files)
            self.assertFalse(any(name in files for name in ['docs/private.env','docs/notes.md','overlay/debug.log','.github/token.key']))

    def test_help_has_no_suppression_placeholder(self):
        with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as error:
            kit.main(['--help'])
        self.assertEqual(error.exception.code,0)
        self.assertNotIn('==SUPPRESS==',output.getvalue())

    def test_raw_templates_require_generation(self):
        for relative in ['kernel-open/nvidia/nv.c','src/nvidia/inc/kernel/gpu/gsp/cmp70_port.h']:
            self.assertIn('#error CMP70HX_TEMPLATE_REQUIRES_GENERATION',(kit.ROOT/'overlay'/relative).read_text())
        from test_kit import config
        with tempfile.TemporaryDirectory() as directory:
            dest=Path(directory); kit.generate_sources(config(),dest)
            for path in dest.rglob('*'):
                if path.is_file():self.assertNotIn('#error CMP70HX_TEMPLATE_REQUIRES_GENERATION',path.read_text())

    @unittest.skipUnless(sys.platform=='linux','C harness runs on Linux')
    def test_actual_overlay_slot_helper_rejects_out_of_range_without_aliasing_gpu_zero(self):
        self.check_slot_helper('overlay/src/nvidia/src/kernel/gpu/gsp/kernel_gsp.c')

    @unittest.skipUnless(sys.platform=='linux','C harness runs on Linux')
    def test_booter_slot_helper_rejects_out_of_range_before_state_access(self):
        relative='overlay/src/nvidia/src/kernel/gpu/gsp/arch/turing/kernel_gsp_booter_tu102.c'
        self.check_slot_helper(relative)
        text=(kit.ROOT/relative).read_text()
        guard=text.index('_kgspCmp90Rejoin14GpuSlot(pGpu, &cmp90PcGpuSlot)')
        first_access=text.index('s_cmp90PcExactTimingAttemptedByGpu[cmp90PcGpuSlot]')
        self.assertLess(guard,first_access)
        self.assertNotIn('/var/lib/cmpunlocker-rs/',text)

    def check_slot_helper(self, relative):
        text=(kit.ROOT/relative).read_text()
        start=text.index('static NV_STATUS\n_kgspCmp90Rejoin14GpuSlot')
        end=text.index('\n}\n',start)+3
        helper=text[start:end]
        harness='''typedef unsigned int NvU32;
typedef int NV_STATUS;
typedef struct { NvU32 gpuInstance; } OBJGPU;
#define CMP90_PC_EXACT_MAX_GPU_INSTANCES 8U
#define NV_OK 0
#define NV_ERR_INVALID_ARGUMENT 1
#define NV_CHECK_OR_RETURN(level,condition,status) do { if (!(condition)) return status; } while (0)
'''+helper+'''
int main(void) {
    OBJGPU gpu;
    NvU32 slot;
    for (gpu.gpuInstance=0; gpu.gpuInstance<8; ++gpu.gpuInstance) {
        slot=99;
        if (_kgspCmp90Rejoin14GpuSlot(&gpu,&slot)!=NV_OK || slot!=gpu.gpuInstance) return 1;
    }
    const NvU32 invalid[]={8,9,255,0xffffffffU};
    for (unsigned int i=0;i<4;++i) {
        gpu.gpuInstance=invalid[i]; slot=99;
        if (_kgspCmp90Rejoin14GpuSlot(&gpu,&slot)==NV_OK || slot!=99) return 2;
    }
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'test.c').write_text(harness)
            subprocess.run(['cc','-Wall','-Wextra','-Werror',str(root/'test.c'),'-o',str(root/'test')],check=True,capture_output=True)
            subprocess.run([str(root/'test')],check=True,capture_output=True)


if __name__=='__main__':unittest.main()
