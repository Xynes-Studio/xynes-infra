#!/usr/bin/env python3
"""Real inert ZIP fixtures verify the native Actions handoff boundary."""
import copy
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
import unittest
import warnings
import zipfile
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
import image_build
import release_manifest
P='/repos/Xynes-Studio/xynes-infra/actions/artifacts/99'
CONTROLLER=dict(repository='Xynes-Studio/xynes-infra',commit='a'*40,workflow='apex-build.yml',run_id=100,run_attempt=1)
ADMISSION=('controller.json','source-manifest.json','public-inputs.json','source-checks.json')
BUILD=('image-receipts.json',)+tuple('archives/shared--'+n+'.oci.tar' for n in image_build.SHARED_BUILD)+tuple('archives/'+s+'--'+n+'.oci.tar' for s in release_manifest.STAGES for n in release_manifest.APPS)
PUBLICATION=('published-receipt.json',)

def bundle(names,method=zipfile.ZIP_STORED):
 out=io.BytesIO()
 with warnings.catch_warnings():
  warnings.simplefilter('ignore',UserWarning)
  with zipfile.ZipFile(out,'w',compression=method) as z:
   for name in names:
    if isinstance(name,tuple):info,data=name
    else:info,data=name,b'{}' if name.endswith('.json') else b'inert OCI bytes'
    z.writestr(info,data)
 return out.getvalue()

def metadata(data,kind='admission'):
 return dict(id=99,name='apex-'+kind+'-100-1',digest='sha256:'+hashlib.sha256(data).hexdigest(),expired=False,size_in_bytes=len(data),url='https://api.github.com'+P,archive_download_url='https://api.github.com'+P+'/zip',workflow_run=dict(id=100,repository_id=1415400917,head_repository_id=1415400917,head_branch='main',head_sha='a'*40))

class Tests(unittest.TestCase):
 def setUp(self):
  path=ROOT/'scripts/release/artifact_handoff.py';self.assertTrue(path.exists(),'artifact handoff implementation missing')
  spec=importlib.util.spec_from_file_location('artifact_handoff',path);self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve();self.output=self.root/'bundle';self.controller=copy.deepcopy(CONTROLLER);self.kind='admission';self.data=bundle(ADMISSION);self.meta=metadata(self.data);self.reference={'id':99,'digest':self.meta['digest']};self.reads=[];self.downloads=[]
 def read(self,path):self.reads.append(path);return copy.deepcopy(self.meta)
 def download(self,aid,destination,max_bytes):
  self.downloads.append((aid,destination,max_bytes));self.assertEqual(aid,99);self.assertTrue(destination.is_absolute());self.assertFalse(destination.exists());self.assertEqual(max_bytes,self.meta['size_in_bytes']);destination.write_bytes(self.data)
 def acquire(self,read=None,download=None):
  return self.module.acquire(kind=self.kind,reference=self.reference,controller=self.controller,read=read or self.read,download=download or self.download,output=self.output)
 def reset_zip(self,data,kind=None):
  if kind:self.kind=kind
  self.data=data;self.meta=metadata(data,self.kind);self.reference={'id':99,'digest':self.meta['digest']};self.reads=[];self.downloads=[]
 def refused(self,read=None,download=None):
  with self.assertRaises(self.module.HandoffError) as c:self.acquire(read,download)
  self.assertEqual(str(c.exception),'Artifact handoff refused: invalid or incomplete bundle')
  self.assertFalse(self.output.exists());self.assertEqual(list(self.root.iterdir()),[])
 def test_admission_build_and_publication_bundles_are_exact_private_files(self):
  completed=[]
  for kind,names in [('admission',ADMISSION),('build',BUILD),('publication',PUBLICATION)]:
   with self.subTest(kind=kind):
    self.output=self.root/kind;self.reset_zip(bundle(names,zipfile.ZIP_DEFLATED),kind)
    self.assertEqual(self.acquire(),self.output);self.assertEqual(self.reads,[P,P]);self.assertEqual(len(self.downloads),1)
    self.assertEqual(stat.S_IMODE(self.output.stat().st_mode),0o700)
    files=[p for p in self.output.rglob('*') if p.is_file()];self.assertEqual({p.relative_to(self.output).as_posix() for p in files},set(names))
    completed.append(kind)
    self.assertTrue(all(stat.S_IMODE(p.stat().st_mode)==0o600 for p in files));self.assertEqual({p.name for p in self.root.iterdir()},set(completed))
 def test_publication_intake_is_small_data_only_and_requires_its_exact_native_name(self):
  self.reset_zip(bundle(PUBLICATION),'publication')
  self.meta['size_in_bytes']=8*1024**2+1
  self.refused();self.assertEqual(self.downloads,[])
  self.reset_zip(bundle(PUBLICATION),'publication');self.meta['name']='apex-build-100-1'
  self.refused();self.assertEqual(self.downloads,[])
  for names in [PUBLICATION+('source.py',),('other-receipt.json',),BUILD]:
   self.reset_zip(bundle(names),'publication');self.refused()
  out=io.BytesIO()
  with zipfile.ZipFile(out,'w') as z:z.writestr('published-receipt.json',b'X'*(256*1024+1))
  self.reset_zip(out.getvalue(),'publication');self.refused()
 def test_migration_artifact_is_one_bounded_private_data_file(self):
  self.reset_zip(bundle([('migration-bundle.tar', b'inert source-bound tar data')]),'migration')
  self.assertEqual(self.acquire(),self.output)
  self.assertEqual({p.name for p in self.output.iterdir()},{'migration-bundle.tar'})
  self.assertEqual((self.output/'migration-bundle.tar').stat().st_mode&0o777,0o600)
 def test_migration_artifact_refuses_oversize_extra_and_executable_inputs(self):
  self.reset_zip(bundle(['migration-bundle.tar']),'migration')
  self.meta['size_in_bytes']=40*1024**2+1
  self.refused();self.assertEqual(self.downloads,[])
  for names in [('migration-bundle.tar','source.py'),('migration-bundle.tar','other.sql'),('migrations.tar',)]:
   self.reset_zip(bundle(names),'migration');self.refused()
  self.reset_zip(bundle([('migration-bundle.tar',b'x'*(32*1024**2+1))]),'migration');self.refused()
 def test_zip64_headers_data_descriptors_and_bounded_central_directory(self):
  out=io.BytesIO()
  with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_DEFLATED) as z:
   for name in ADMISSION:
    with z.open(name,'w',force_zip64=True) as f:f.write(b'{}')
  raw=out.getvalue();end=len(raw)-22;fields=struct.unpack('<4s4H2IH',raw[end:]);offset=fields[6];length=fields[5];count=fields[4]
  zip64=struct.pack('<4sQ2H2I4Q',b'PK\x06\x06',44,45,45,0,0,count,count,length,offset)
  locator=struct.pack('<4sIQI',b'PK\x06\x07',0,end,1)
  tail=struct.pack('<4s4H2IH',b'PK\x05\x06',0,0,65535,65535,0xffffffff,0xffffffff,0)
  self.reset_zip(raw[:end]+zip64+locator+tail);self.assertEqual(self.acquire(),self.output)
 def test_data_descriptor_streaming_zip_is_accepted(self):
  class NonSeekable(io.BytesIO):
   def seek(self,*args):raise io.UnsupportedOperation('no seeking')
  out=NonSeekable()
  with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_DEFLATED) as z:
   for name in ADMISSION:z.writestr(name,b'{}')
  self.reset_zip(out.getvalue());self.assertEqual(self.acquire(),self.output)
 def test_central_directory_bounds_multidisk_truncation_and_duplicate_header_offsets(self):
  original=bundle(ADMISSION);end=len(original)-22
  for field,value in [(4,1),(8,999),(12,65537),(16,0)]:
   raw=bytearray(original);struct.pack_into('<H' if field in (4,8) else '<I',raw,end+field,value);self.reset_zip(bytes(raw));self.refused()
  self.reset_zip(original[:-1]);self.refused()
  self.reset_zip(original+b'trailing payload');self.refused()
  raw=bytearray(original);first=raw.index(b'PK\x01\x02');second=raw.index(b'PK\x01\x02',first+4);struct.pack_into('<I',raw,second+42,0);self.reset_zip(bytes(raw));self.refused()
 def test_hardlinked_download_is_refused_and_unowned_link_is_preserved(self):
  outside=self.root/'preserved';outside.write_bytes(self.data)
  def hardlink(aid,path,limit):os.link(outside,path)
  with self.assertRaises(self.module.HandoffError):self.acquire(download=hardlink)
  self.assertEqual(outside.read_bytes(),self.data);self.assertFalse(self.output.exists());self.assertEqual(outside.stat().st_nlink,2)
 def test_substituted_output_file_is_preserved_on_failure(self):
  original=self.module.zipfile.ZipFile.open;calls=0
  def replace_then_fail(z,*args,**kwargs):
   nonlocal calls
   calls+=1
   if calls==2:
    target=self.output/'controller.json';target.rename(self.output/'retained-original')
    target.write_bytes(b'external replacement');raise RuntimeError('redacted')
   return original(z,*args,**kwargs)
  with patch.object(self.module.zipfile.ZipFile,'open',replace_then_fail):
   with self.assertRaises(self.module.HandoffError):self.acquire()
  self.assertEqual((self.output/'controller.json').read_bytes(),b'external replacement');self.assertEqual((self.output/'retained-original').read_bytes(),b'{}')
 def test_controller_reference_and_complete_native_metadata_refuse_before_download(self):
  mutations=[('controller','run_attempt',2),('controller','commit','invalid'),('reference','id',True),('reference','digest','a'*64),('reference','extra',True),('meta','id',True),('meta','id',98),('meta','name','apex-admission-100-2'),('meta','expired',True),('meta','expired',0),('meta','size_in_bytes',True),('meta','size_in_bytes',0),('meta','size_in_bytes',32*1024**3+1),('meta','digest','sha256:'+'b'*64),('meta','url','https://foreign.invalid/secret'),('meta','archive_download_url','https://api.github.com'+P+'/zip?secret=1'),('run','id',101),('run','repository_id',1),('run','head_repository_id',1),('run','head_branch','develop'),('run','head_sha','b'*40)]
  for target,key,value in mutations:
   with self.subTest(target=target,key=key,value=value):
    self.controller=copy.deepcopy(CONTROLLER);self.reset_zip(bundle(ADMISSION));obj={'controller':self.controller,'reference':self.reference,'meta':self.meta,'run':self.meta['workflow_run']}[target];obj[key]=value
    self.refused();self.assertEqual(self.downloads,[])
  for key in ['id','name','digest','expired','size_in_bytes','url','archive_download_url','workflow_run']:
   self.reset_zip(bundle(ADMISSION));del self.meta[key];self.refused();self.assertEqual(self.downloads,[])
  self.reset_zip(bundle(ADMISSION));self.kind='other';self.refused();self.assertEqual(self.downloads,[])
 def test_entire_zip_hash_and_native_size_are_verified(self):
  self.data=self.data[:-1]+b'X';self.refused()
  self.reset_zip(bundle(ADMISSION));self.meta['size_in_bytes']+=1;self.refused()
 def test_late_native_metadata_drift_cleans_exact_owned_download(self):
  count=0
  def drift(path):
   nonlocal count
   count+=1;result=self.read(path)
   if count==2:result['expired']=True
   return result
  self.refused(read=drift);self.assertEqual(self.reads,[P,P])
 def test_all_items_are_inspected_before_first_extraction(self):
  for extra in ['unexpected.json','../escape','/absolute','archives/','archives/../escape','source-manifest.json/child','controller.json']:
   self.reset_zip(bundle(ADMISSION+(extra,)))
   with patch.object(self.module.zipfile.ZipFile,'open',side_effect=AssertionError('no payload extraction before all metadata checks')):self.refused()
  self.reset_zip(bundle(ADMISSION[:-1]));self.refused()
  self.reset_zip(bundle(BUILD[:-1]),'build');self.refused()
 def test_unix_symlink_directory_other_types_and_bad_permissions_are_refused(self):
  for file_type in [stat.S_IFLNK,stat.S_IFDIR,stat.S_IFIFO,stat.S_IFCHR,stat.S_IFSOCK]:
   info=zipfile.ZipInfo('controller.json');info.create_system=3;info.external_attr=(file_type|0o600)<<16
   self.reset_zip(bundle(((info,b'{}'),)+ADMISSION[1:]));self.refused()
 def test_encryption_unsupported_method_sizes_bombs_and_crc_are_refused(self):
  for method in [zipfile.ZIP_BZIP2,zipfile.ZIP_LZMA]:self.reset_zip(bundle(ADMISSION,method));self.refused()
  original=bundle(ADMISSION)
  for field,value in [(8,1),(10,99),(24,4*1024*1024+1)]:
   raw=bytearray(original);offset=raw.index(b'PK\x01\x02');struct.pack_into('<H' if field in (8,10) else '<I',raw,offset+field,value);self.reset_zip(bytes(raw));self.refused()
  self.reset_zip(bundle((('controller.json',b'X'*(4*1024*1024)),)+ADMISSION[1:],zipfile.ZIP_DEFLATED));self.refused()
  raw=bytearray(original);at=raw.index(b'{}');raw[at]=ord('!');self.reset_zip(bytes(raw));self.refused()
 def test_preexisting_output_symlink_or_relative_parent_is_never_modified(self):
  self.output.mkdir();(self.output/'keep').write_bytes(b'keep')
  with self.assertRaises(self.module.HandoffError):self.acquire()
  self.assertEqual((self.output/'keep').read_bytes(),b'keep');self.assertEqual(self.downloads,[])
  self.output=self.root/'alias';self.output.symlink_to(self.root/'bundle',target_is_directory=True)
  with self.assertRaises(self.module.HandoffError):self.acquire()
  self.assertTrue(self.output.is_symlink());self.assertEqual(self.downloads,[])
  self.output=Path('relative')
  with self.assertRaises(self.module.HandoffError):self.acquire()
 def test_download_callback_errors_or_unsafe_files_are_redacted(self):
  def raises(aid,path,limit):path.write_bytes(self.data);raise RuntimeError('private URL token /secret/path')
  self.refused(download=raises)
  def fifo(aid,path,limit):os.mkfifo(path)
  # A malicious callback is outside the trust contract; the created FIFO is
  # retained rather than opened or recursively removed.
  with self.assertRaises(self.module.HandoffError) as c:self.acquire(download=fifo)
  self.assertEqual(str(c.exception),'Artifact handoff refused: invalid or incomplete bundle');self.assertFalse(self.output.exists())
 def test_owned_extraction_failure_cleanup_and_substituted_file_preservation(self):
  original=self.module.zipfile.ZipFile.open;calls=0
  def error(z,*args,**kwargs):
   nonlocal calls
   calls+=1
   if calls==2:raise RuntimeError('secret response')
   return original(z,*args,**kwargs)
  with patch.object(self.module.zipfile.ZipFile,'open',error):self.refused()
  self.assertEqual(calls,2)

if __name__=='__main__':unittest.main()
