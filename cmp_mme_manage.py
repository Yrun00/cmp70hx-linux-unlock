#!/usr/bin/python3
"""Exact-hash NVIDIA610.43.03 userspace patch/restore; no NVIDIA API calls."""
import argparse,hashlib,json,os,stat,sys,tempfile
from pathlib import Path,PurePosixPath

VERSION='610.43.03'
ENTRIES=[
 {'name':'libnvidia-eglcore.so.610.43.03','original':'35517c07dc35c1d966f7c8102deca9cd1f4925f689f95b9ffacbfada3ef6e8f8','patched':'3ef666227c96820e6c16e0e72ef57e837b3ed7f40adeeeaacf7c2aa2e6cc6408','offsets':[0xad5f70,0xc65014]},
 {'name':'libnvidia-glcore.so.610.43.03','original':'6304dff2ee2e1ced2febf1e8b720f16c3cfdb698d4c8cc20f340bd1f09556687','patched':'dd622f7c9fc9b3e01a67e9be09fe70589dead9ae298b919074bb99561bc69a20','offsets':[0xb20861,0xdcbb74]},
]
def sha(data):return hashlib.sha256(data).hexdigest()
def attributes(path):
 return {name:os.getxattr(path,name) for name in os.listxattr(path)}
class Manager:
 def __init__(self,root=Path('/'),entries=ENTRIES,lib_dir=None):
  directory=PurePosixPath(lib_dir or '/usr/lib/x86_64-linux-gnu')
  if not directory.is_absolute() or '..' in directory.parts:raise ValueError('Absolute library directory required')
  self.root=Path(root);self.entries=entries;self.libs=self.root/str(directory).lstrip('/');self.state=self.root/'var/lib/cmp-mme';self.backups=self.state/'original'/VERSION
 def inspect(self):
  result=[]
  for e in self.entries:
   p=self.libs/e['name'];digest=sha(p.read_bytes()) if p.is_file() else None
   state='original' if digest==e['original'] else 'patched' if digest==e['patched'] else 'unknown'
   b=self.backups/e['name'];result.append({'name':e['name'],'state':state,'sha256':digest,'backup_sha256':sha(b.read_bytes()) if b.is_file() else None})
  version=self.root/'sys/module/nvidia/version'
  return {'version_expected':VERSION,'kernel_version':version.read_text().strip() if version.exists() else None,'files':result}
 def _atomic(self,path,data,mode=0o600,uid=None,gid=None,attrs=None):
  path.parent.mkdir(parents=True,exist_ok=True)
  fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'.cmp-mme-',dir=path.parent)
  try:
   with os.fdopen(fd,'wb') as f:
    f.write(data);f.flush()
    if uid is not None:os.fchown(f.fileno(),uid,gid)
    os.fchmod(f.fileno(),mode)
    if attrs is not None:
     for name in os.listxattr(f.fileno()):
      if name not in attrs:os.removexattr(f.fileno(),name)
     for name,value in attrs.items():os.setxattr(f.fileno(),name,value)
     if attributes(f.fileno())!=attrs:raise RuntimeError('Library metadata readback mismatch')
     st=os.fstat(f.fileno())
     if (stat.S_IMODE(st.st_mode),st.st_uid,st.st_gid)!=(mode,uid,gid):raise RuntimeError('Library permissions readback mismatch')
    os.fsync(f.fileno())
   os.replace(tmp,path)
   d=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
   try:os.fsync(d)
   finally:os.close(d)
  finally:
   if os.path.exists(tmp):os.unlink(tmp)
 def transact(self,action):
  assert action in ['apply','restore']
  if action=='apply':
   version=self.root/'sys/module/nvidia/version'
   if not version.exists() or version.read_text().strip()!=VERSION:raise RuntimeError('Unsupported/not-loaded kernel driver;no library writes')
  plans=[]
  # Validate BOTH inputs and existing backups before writing either live library.
  for e in self.entries:
   p=self.libs/e['name']
   if p.is_symlink() or not p.is_file():raise RuntimeError('Expected regular installed file:'+str(p))
   old=p.read_bytes();digest=sha(old)
   if digest not in [e['original'],e['patched']]:raise RuntimeError('Unrecognized input hash:'+e['name'])
   original=bytearray(old)
   if digest==e['patched']:
    for off in e['offsets']:
     if original[off-4:off+4]!=bytes.fromhex('680e012001000000'):raise RuntimeError('Patched packet mismatch')
     original[off]=240
   if sha(original)!=e['original']:raise RuntimeError('Original reconstruction hash mismatch')
   backup=self.backups/e['name']
   if backup.exists() and (backup.is_symlink() or sha(backup.read_bytes())!=e['original']):raise RuntimeError('Bad original backup:'+e['name'])
   candidate=bytearray(original)
   if action=='apply':
    if candidate.count(bytes.fromhex('680e0120f0000000'))!=2:raise RuntimeError('Expected exactly two emitter packets')
    for off in e['offsets']:
     if candidate[off-4:off+4]!=bytes.fromhex('680e0120f0000000'):raise RuntimeError('Original packet mismatch')
     candidate[off]=1
   expected=e['patched'] if action=='apply' else e['original']
   if sha(candidate)!=expected:raise RuntimeError('Candidate hash mismatch')
   if action=='apply' and sum(a!=b for a,b in zip(original,candidate))!=2:raise RuntimeError('Expected two changed bytes')
   attrs=attributes(p)
   if {'security.ima','security.evm'} & attrs.keys():raise RuntimeError('IMA/EVM-protected library unsupported; no live library writes:'+e['name'])
   plans.append((e,p,old,bytes(original),bytes(candidate),p.stat(),attrs))
  for e,p,old,original,candidate,st,attrs in plans:
   backup=self.backups/e['name']
   if not backup.exists():self._atomic(backup,original)
  replaced=[]
  try:
   for e,p,old,original,candidate,st,attrs in plans:
    if sha(p.read_bytes())!=sha(old):raise RuntimeError('Input changed during transaction:'+e['name'])
    current=p.stat()
    if attributes(p)!=attrs or (current.st_mode,current.st_uid,current.st_gid)!=(st.st_mode,st.st_uid,st.st_gid):raise RuntimeError('Input metadata changed during transaction:'+e['name'])
    if old==candidate:continue
    replaced.append((p,old,st,attrs))
    self._atomic(p,candidate,stat.S_IMODE(st.st_mode),st.st_uid,st.st_gid,attrs=attrs)
  except BaseException:
   for p,old,st,attrs in reversed(replaced):
    if p.read_bytes()!=old:self._atomic(p,old,stat.S_IMODE(st.st_mode),st.st_uid,st.st_gid,attrs=attrs)
   raise
  result=self.inspect();result.update(action=action,changed_files=[p.name for p,old,st,attrs in replaced],boot_id=(self.root/'proc/sys/kernel/random/boot_id').read_text().strip())
  self._atomic(self.state/'last-result.json',(json.dumps(result,indent=2)+'\n').encode(),0o644)
  return result
def main():
 parser=argparse.ArgumentParser();parser.add_argument('action',choices=['status','apply','restore']);parser.add_argument('--lib-dir',default='/usr/lib/x86_64-linux-gnu');args=parser.parse_args();m=Manager(lib_dir=args.lib_dir)
 if args.action=='status':print(json.dumps(m.inspect()));return
 if os.geteuid()!=0:raise SystemExit('root required for apply/restore')
 m.state.mkdir(parents=True,exist_ok=True)
 import fcntl
 with (m.state/'lock').open('a') as f:
  fcntl.flock(f,fcntl.LOCK_EX);print(json.dumps(m.transact(args.action)))
if __name__=='__main__':
 try:main()
 except Exception as exc:
  print(json.dumps({'status':'failed','error':str(exc),'note':'Inputs hash-gated;inspect status after any transaction/storage failure'}),file=sys.stderr);raise SystemExit(1)
