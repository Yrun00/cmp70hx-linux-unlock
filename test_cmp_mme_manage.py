"""CPU transaction tests, run on Linux in an ordinary temporary directory."""
import importlib.util,hashlib,tempfile,unittest,copy,os
import struct
from unittest.mock import patch
from pathlib import Path
spec=importlib.util.spec_from_file_location('manager',Path(__file__).with_name('cmp_mme_manage.py'));mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
@unittest.skipUnless(os.name == 'posix', 'Real fsync/chown transaction tests require Linux')
class Transactions(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.entries=[]
  for index in range(2):
   raw=bytes([index+9])*8+bytes.fromhex('680e0120f0000000')+bytes([index+9])*8+bytes.fromhex('680e0120f0000000');new=bytearray(raw);new[12]=new[28]=1
   e={'name':'core'+str(index),'original':mod.sha(raw),'patched':mod.sha(new),'offsets':[12,28]};self.entries.append(e)
   p=self.root/'usr/lib/x86_64-linux-gnu'/e['name'];p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw)
  for name,value in [('sys/module/nvidia/version',mod.VERSION),('proc/sys/kernel/random/boot_id','fixture-boot')]:
   p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(value)
  self.m=mod.Manager(self.root,self.entries)
 def tearDown(self):self.tmp.cleanup()
 def test_apply_idempotent_restore(self):
  self.assertEqual(len(self.m.transact('apply')['changed_files']),2)
  inodes=[(self.m.libs/e['name']).stat().st_ino for e in self.entries]
  self.assertEqual(self.m.transact('apply')['changed_files'],[])
  self.assertEqual(inodes,[(self.m.libs/e['name']).stat().st_ino for e in self.entries])
  self.m.transact('restore');self.assertTrue(all(x['state']=='original' for x in self.m.inspect()['files']))
 def test_second_unknown_refuses_both(self):
  (self.m.libs/self.entries[1]['name']).write_bytes(b'unknown')
  with self.assertRaises(RuntimeError):self.m.transact('apply')
  self.assertEqual(self.m.inspect()['files'][0]['state'],'original');self.assertFalse(self.m.backups.exists())
 def test_second_write_failure_rolls_back_first(self):
  original=self.m._atomic;failed=False
  def injected(path,*args,**kwargs):
   nonlocal failed
   if path==self.m.libs/self.entries[1]['name'] and not failed:failed=True;raise OSError('injected second live write failure')
   return original(path,*args,**kwargs)
  self.m._atomic=injected
  with self.assertRaises(OSError):self.m.transact('apply')
  self.assertTrue(all(x['state']=='original' for x in self.m.inspect()['files']))
 def test_unknown_version_and_bad_backup_refused(self):
  (self.root/'sys/module/nvidia/version').write_text('unknown')
  with self.assertRaises(RuntimeError):self.m.transact('apply')
  (self.root/'sys/module/nvidia/version').write_text(mod.VERSION);self.m.transact('apply')
  (self.m.backups/self.entries[1]['name']).write_bytes(b'bad')
  with self.assertRaises(RuntimeError):self.m.transact('restore')
  self.assertTrue(all(x['state']=='patched' for x in self.m.inspect()['files']))
 def test_xattrs_and_posix_acl_survive_apply_and_restore(self):
  path=self.m.libs/self.entries[0]['name']
  acl=struct.pack('<I',2)+b''.join(struct.pack('<HHI',tag,perm,uid) for tag,perm,uid in
      [(1,7,0xffffffff),(2,4,12345),(4,4,0xffffffff),(16,4,0xffffffff),(32,0,0xffffffff)])
  os.setxattr(path,'user.cmp70hx-test',b'preserved')
  os.setxattr(path,'system.posix_acl_access',acl)
  before=mod.attributes(path);mode=path.stat().st_mode
  self.m.transact('apply');self.m.transact('restore')
  self.assertEqual(mod.attributes(path),before)
  self.assertEqual(path.stat().st_mode,mode)
 def test_integrity_signed_libraries_are_refused_before_live_writes(self):
  with patch.object(mod,'attributes',return_value={'security.ima':b'signature'}):
   with self.assertRaisesRegex(RuntimeError,'IMA/EVM'):self.m.transact('apply')
  self.assertTrue(all(x['state']=='original' for x in self.m.inspect()['files']))
  self.assertFalse(self.m.backups.exists())
 def test_metadata_failure_preserves_original_and_rolls_back_previous_library(self):
  for e in self.entries:os.setxattr(self.m.libs/e['name'],'user.test',b'value')
  original=os.setxattr;count=0
  def fail(*args,**kwargs):
   nonlocal count
   count+=1
   if count==2:raise PermissionError('metadata denied')
   return original(*args,**kwargs)
  with patch.object(mod.os,'setxattr',side_effect=fail):
   with self.assertRaises(PermissionError):self.m.transact('apply')
  self.assertTrue(all(x['state']=='original' for x in self.m.inspect()['files']))
  for e in self.entries:self.assertEqual(os.getxattr(self.m.libs/e['name'],'user.test'),b'value')
if __name__=='__main__':unittest.main()
