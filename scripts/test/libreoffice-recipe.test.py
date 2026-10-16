#!/usr/bin/env python3
"""Exercise the recipe's explicit log deletion in an isolated filesystem."""
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
LOGS={'/var/log/apt/history.log','/var/log/apt/term.log','/var/log/apt/eipp.log.xz','/var/log/dpkg.log','/var/log/alternatives.log'}

class Tests(unittest.TestCase):
 def test_removes_only_generated_install_logs_preserving_runtime_package_proof(self):
  recipe=(ROOT/'infra/images/libreoffice.Dockerfile').read_text()
  commands=[shlex.split(line.strip().removesuffix('\\').strip())[1:] for line in recipe.splitlines() if line.strip().startswith('&& rm -f /var/log/')]
  self.assertEqual(len(commands),1,'volatile package installation logs remain in the image')
  command=commands[0];self.assertEqual(command[:2],['rm','-f']);self.assertEqual(set(command[2:]),LOGS)
  with tempfile.TemporaryDirectory() as directory:
   base=Path(directory);protected={'/usr/share/xynes/native-packages.txt','/var/lib/dpkg/status','/usr/share/keyrings/debian-archive-keyring.gpg','/etc/apt/sources.list.d/xynes-snapshot.sources','/var/log/runtime.log'}
   for name in LOGS|protected:
    path=base/name.lstrip('/');path.parent.mkdir(parents=True,exist_ok=True);path.write_text('preserve package or remove generated log')
   subprocess.run(command[:2]+[str(base/path.lstrip('/')) for path in command[2:]],check=True)
   self.assertTrue(all(not (base/name.lstrip('/')).exists() for name in LOGS))
   self.assertTrue(all((base/name.lstrip('/')).exists() for name in protected))
  self.assertLess(recipe.index('dpkg-query -W'),recipe.index('&& rm -f /var/log/'))
  self.assertIn('libreoffice-core=4:7.4.7-1+deb12u14',recipe)
  self.assertIn('Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg',recipe)
  self.assertNotIn('--allow-unauthenticated',recipe);self.assertNotIn('trusted=yes',recipe)
 def test_removes_only_generated_font_and_linker_auxiliary_caches(self):
  recipe=(ROOT/'infra/images/libreoffice.Dockerfile').read_text()
  expected="&& find /var/cache/fontconfig -maxdepth 1 -type f -name '*-le64.cache-8' -delete"
  self.assertIn(expected,recipe,'font caches embed build filesystem identity')
  self.assertIn('&& rm -f /var/cache/ldconfig/aux-cache',recipe)
  with tempfile.TemporaryDirectory() as directory:
   base=Path(directory);font=base/'var/cache/fontconfig';font.mkdir(parents=True)
   generated=font/'fixture-le64.cache-8';generated.write_text('volatile inode and ctime')
   protected=[font/'CACHEDIR.TAG',font/'unknown-runtime-cache',base/'etc/ld.so.cache',base/'usr/share/fonts/truetype/font.ttf',base/'etc/fonts/fonts.conf']
   for path in protected:path.parent.mkdir(parents=True,exist_ok=True);path.write_text('runtime data')
   auxiliary=base/'var/cache/ldconfig/aux-cache';auxiliary.parent.mkdir(parents=True);auxiliary.write_text('volatile filesystem cache')
   argv=shlex.split(expected)[1:];argv[1]=str(font);subprocess.run(argv,check=True)
   subprocess.run(['rm','-f',str(auxiliary)],check=True)
   self.assertFalse(generated.exists());self.assertFalse(auxiliary.exists());self.assertTrue(all(path.exists() for path in protected))
  self.assertIn('XDG_CACHE_HOME=/tmp/.cache',recipe)

if __name__=='__main__':unittest.main()
