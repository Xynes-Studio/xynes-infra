#!/usr/bin/env python3
"""Real inert child processes: no hosted database or Docker daemon acceptance."""
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT/'scripts/release/pg_container_bridge.py'
DB_IMAGE = 'postgres:17-alpine@sha256:5a6fcbc5d93831991d2386fa634509b3c49a1ac5ffb70c13c2322840f821d7e7'
CID = 'a'*64


class Tests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.is_file(), 'Docker PostgreSQL bridge is missing')
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.work = self.root/'backups'; self.work.mkdir(mode=0o700)
        self.dumpdir = self.work/'.backup-fixture'; self.dumpdir.mkdir(mode=0o700)
        self.dump = self.dumpdir/'database.dump'; self.dump.write_bytes(b'PGDMP\x00inert-dump')
        self.dump.chmod(0o600)
        self.log = self.root/'argv.jsonl'
        self.meta = self.root/'meta.json'
        self.row = {'id': CID, 'image': DB_IMAGE, 'labels': {
            'com.docker.compose.project': 'xynes-qa', 'com.docker.compose.service': 'supabase-db',
            'com.docker.compose.oneoff': 'False', 'com.xynes.managed': 'true',
            'com.xynes.environment': 'qa', 'com.xynes.role': 'infrastructure'},
            'state': 'running', 'health': 'healthy'}
        self.meta.write_text(json.dumps(self.row))
        self.docker = self.root/'docker-fixture'
        self.docker.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with open(%r, 'a') as log: log.write(json.dumps(args)+'\\n')
assert args[:4] == ['--host','unix:///var/run/docker.sock','--config','/etc/xynes/deploy/docker']
args = args[4:]
if args[0] == 'ps':
    print(%r)
elif args[0] == 'inspect':
    print(pathlib.Path(%r).read_text())
elif args[0] == 'exec':
    assert '-t' not in args
    assert os.environ.get('PGPASSWORD') == 'inert-private-db-password'
    assert 'UNRELATED_SECRET' not in os.environ
    assert 'DOCKER_HOST' not in os.environ
    program = args[args.index(%r)+1]
    flags = args[args.index(%r)+2:]
    if program == 'psql':
        if '--command' in flags: print('[]')
        else:
            for line in sys.stdin:
                if 'pg_export_snapshot' in line: print('00000001-00000002-1',flush=True)
                else: print('[]',flush=True)
    elif program == 'pg_dump':
        if '--version' in flags: print('pg_dump (PostgreSQL) 17.10')
        else: sys.stdout.buffer.write(b'PGDMP'+bytes(range(256))*32)
    elif program == 'pg_restore':
        data = sys.stdin.buffer.read()
        assert data == b'PGDMP\\x00inert-dump'
        assert flags == ['--schema-only','--no-owner','--no-acl','--file=-']
        print('CREATE SCHEMA inert;')
    else: sys.exit(1)
else: sys.exit(1)
''' % (str(self.log), CID, str(self.meta), CID, CID))
        self.docker.chmod(0o700)
        self.entry = self.root/'entry.py'
        self.entry.write_text('''import importlib.util, os, pathlib, sys
spec=importlib.util.spec_from_file_location('pg_bridge',%r)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.BASE[0]=%r
bridge=m.Bridge('qa',work_root=pathlib.Path(%r),owner_uid=os.geteuid(),
 environment_loader=lambda stage: {'XYNES_ENV':'qa','POSTGRES_PASSWORD':'inert-private-db-password'})
raise SystemExit(bridge.run(sys.argv[1],sys.argv[2:],input_fd=0,output_fd=1))
''' % (str(MODULE), str(self.docker), str(self.work)))
        self.env = {**os.environ, 'UNRELATED_SECRET':'INERT_SENTINEL', 'DOCKER_HOST':'tcp://hostile.invalid',
                    'PGPASSWORD':'WRONG_INHERITED_PASSWORD', 'PYTHONDONTWRITEBYTECODE':'1'}

    def call(self, program, args, data=b''):
        return subprocess.run([sys.executable,'-I',str(self.entry),program,*args], input=data,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=self.env,timeout=10)

    def refuse(self, program, args, data=b''):
        result=self.call(program,args,data)
        self.assertEqual(result.returncode,1, result.stderr)
        self.assertEqual(result.stderr,b'PostgreSQL container bridge refused\n')
        self.assertNotIn(b'INERT_SENTINEL', result.stdout+result.stderr)
        self.assertNotIn(b'inert-private-db-password', result.stdout+result.stderr)

    def test_persistent_psql_snapshot_roundtrip_preserves_stdin_and_stdout(self):
        process=subprocess.Popen([sys.executable,'-I',str(self.entry),'psql','-X','-qAt','--no-password',
            '--set=ON_ERROR_STOP=1'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=self.env)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        process.stdin.write(b"BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SET LOCAL search_path=pg_catalog; SET LOCAL TIME ZONE 'UTC'; SET LOCAL DateStyle='ISO'; SET LOCAL extra_float_digits=3; SET LOCAL statement_timeout=1800000; SELECT pg_export_snapshot();\n")
        process.stdin.flush()
        self.assertEqual(process.stdout.readline(),b'00000001-00000002-1\n')
        process.stdin.write(b"SELECT coalesce(json_agg(nspname ORDER BY nspname), '[]') FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema';\n")
        process.stdin.flush()
        self.assertEqual(process.stdout.readline(),b'[]\n')
        process.stdin.close()
        self.assertEqual(process.wait(timeout=5),0)
        self.assertEqual(process.stderr.read(),b'')
        process.stdout.close();process.stderr.close()

    def test_dump_stream_and_schema_only_local_dump(self):
        result=self.call('pg_dump',['--no-password','--format=custom','--no-owner','--no-acl','--snapshot','00000001-00000002-1'])
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout,b'PGDMP'+bytes(range(256))*32)
        schema=self.call('pg_restore',['--schema-only','--no-owner','--no-acl','--file=-',str(self.dump)])
        self.assertEqual(schema.returncode,0,schema.stderr)
        self.assertEqual(schema.stdout,b'CREATE SCHEMA inert;\n')
        argv=self.log.read_text()
        self.assertNotIn('inert-private-db-password',argv)
        self.assertNotIn(str(self.dump),argv)
        self.assertNotIn('INERT_SENTINEL',argv)

    def test_arbitrary_sql_flags_programs_and_hosted_restore_are_denied(self):
        exact=['-X','-qAt','--no-password','--set=ON_ERROR_STOP=1']
        for args in (exact+['--command','DROP TABLE platform.workspaces;'],exact+['-h','prod'],['--help']):
            self.refuse('psql',args)
        for sql in (b'DROP TABLE platform.workspaces;\n',b'SELECT current_setting(\'password\');\n',b'\\!id\n',b'SELECT 1;\n',b'A'*1048577):
            self.refuse('psql',exact,sql)
        self.refuse('pg_restore',['--no-password','--dbname','postgres',str(self.dump)])
        self.refuse('pg_dump',['--format=custom','--dbname','postgres'])
        self.refuse('sh',['-c','id'])

    def test_wrong_container_ownership_image_state_or_multiple_ids_are_denied(self):
        for field,value in [('image','postgres:latest'),('id','b'*64),('health','unhealthy'),('state','exited')]:
            row={**self.row,field:value};self.meta.write_text(json.dumps(row))
            self.refuse('pg_dump',['--version'])
        for key,value in [('com.xynes.environment','prod'),('com.xynes.managed','false'),
                          ('com.docker.compose.service','other'),('com.docker.compose.project','xynes-prod')]:
            row={**self.row,'labels':{**self.row['labels'],key:value}};self.meta.write_text(json.dumps(row))
            self.refuse('pg_dump',['--version'])
        self.meta.write_text(json.dumps(self.row))
        script=self.docker.read_text().replace('print(%r)' % CID,'print(%r)' % (CID+'\\n'+'b'*64))
        self.docker.write_text(script)
        self.refuse('pg_dump',['--version'])

    def test_schema_dump_path_permissions_and_links_are_checked(self):
        base=['--schema-only','--no-owner','--no-acl','--file=-']
        self.refuse('pg_restore',base+['/etc/shadow'])
        self.dump.chmod(0o644);self.refuse('pg_restore',base+[str(self.dump)])
        self.dump.chmod(0o600)
        link=self.dumpdir/'other';os.link(self.dump,link)
        self.refuse('pg_restore',base+[str(self.dump)])
        link.unlink()
        original=self.dump.read_bytes();self.dump.unlink()
        other=self.root/'outside.dump';other.write_bytes(original);other.chmod(0o600)
        self.dump.symlink_to(other)
        self.refuse('pg_restore',base+[str(self.dump)])

    def test_fixed_catalog_query_and_version_remain_usable(self):
        query="SET search_path=pg_catalog; SET TIME ZONE 'UTC'; SET DateStyle='ISO'; SET extra_float_digits=3; SET statement_timeout=1800000; SELECT coalesce(json_agg(nspname ORDER BY nspname), '[]') FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema';"
        result=self.call('psql',['-X','-qAt','--no-password','--set=ON_ERROR_STOP=1','--command',query])
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout,b'[]\n')
        result=self.call('pg_dump',['--version'])
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn(b'17.10',result.stdout)

    def test_external_errors_and_output_limits_keep_diagnostics_redacted(self):
        script=self.docker.read_text().replace("elif args[0] == 'exec':", "elif args[0] == 'exec':\n    print('INERT_SENTINEL inert-private-db-password',file=sys.stderr)\n    sys.exit(1)")
        self.docker.write_text(script)
        self.refuse('pg_dump',['--version'])
        self.assertNotIn('inert-private-db-password',self.log.read_text())
        self.docker.write_text(script.replace("    sys.exit(1)", "    sys.stdout.buffer.write(b'A'*65537)\n    sys.exit(0)",1))
        self.refuse('pg_dump',['--version'])

    def test_timeout_refuses_the_own_child_without_leaking_errors(self):
        script=self.docker.read_text().replace("elif args[0] == 'exec':", "elif args[0] == 'exec':\n    import time\n    time.sleep(30)")
        self.docker.write_text(script)
        self.entry.write_text(self.entry.read_text().replace("m.BASE[0]=", "m.TIMEOUT=0.1\nm.BASE[0]="))
        started=time.monotonic()
        self.refuse('pg_dump',['--version'])
        self.assertLess(time.monotonic()-started,5)

    def test_wrapper_interruption_closes_snapshot_stdin_in_the_child(self):
        marker=self.root/'snapshot-eof'
        script=self.docker.read_text().replace("                else: print('[]',flush=True)", "                else: print('[]',flush=True)\n            pathlib.Path(%r).write_text('snapshot stdin closed')" % str(marker))
        self.docker.write_text(script)
        process=subprocess.Popen([sys.executable,'-I',str(self.entry),'psql','-X','-qAt','--no-password','--set=ON_ERROR_STOP=1'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=self.env)
        process.stdin.write(b"BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SET LOCAL search_path=pg_catalog; SET LOCAL TIME ZONE 'UTC'; SET LOCAL DateStyle='ISO'; SET LOCAL extra_float_digits=3; SET LOCAL statement_timeout=1800000; SELECT pg_export_snapshot();\n")
        process.stdin.flush()
        self.assertEqual(process.stdout.readline(),b'00000001-00000002-1\n')
        process.kill();process.wait(timeout=5)
        process.stdin.close();process.stdout.close();process.stderr.close()
        deadline=time.monotonic()+5
        while not marker.exists() and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertTrue(marker.exists(),'snapshot stdin must not remain held by an orphan wrapper')

    def test_container_libpq_target_and_service_overrides_are_explicit(self):
        result=self.call('pg_dump',['--version'])
        self.assertEqual(result.returncode,0,result.stderr)
        calls=[json.loads(line) for line in self.log.read_text().splitlines()]
        argv=calls[-1]
        for value in ('PGHOST=127.0.0.1','PGPORT=5432','PGDATABASE=postgres','PGUSER=postgres',
                      'PGSERVICE=','PGSERVICEFILE=','PGHOSTADDR=','PGOPTIONS=',
                      'PGPASSFILE=/nonexistent','PGSSLMODE=disable','PGCONNECT_TIMEOUT=15'):
            self.assertIn(value,argv)

    def test_existing_backup_tool_uses_fixed_wrappers_without_source_changes(self):
        script=self.docker.read_text()
        replacement="""    def query(line):
        if 'pg_export_snapshot' in line: return '00000001-00000002-1'
        if 'json_agg(n.nspname' in line: return json.dumps(['platform.workspaces'])
        if 'json_agg(nspname' in line: return json.dumps(['platform','public'])
        if "'ordinal'" in line: return '[]'
        if "'row_md5'" in line: return json.dumps({'rows':2,'row_md5':'a'*32})
        raise RuntimeError('unexpected fixture query')
    if program == 'psql':
        if '--command' in flags: print(query(flags[flags.index('--command')+1]))
        else:
            for line in sys.stdin: print(query(line),flush=True)
"""
        start=script.index("    if program == 'psql':")
        end=script.index("    elif program == 'pg_dump':",start)
        script=script[:start]+replacement+script[end:]
        script=script.replace("assert data == b'PGDMP\\x00inert-dump'", "assert data.startswith(b'PGDMP')")
        self.docker.write_text(script)
        wrappers=self.root/'wrappers';wrappers.mkdir(mode=0o700)
        entry=self.entry.read_text()
        for program in ('psql','pg_dump','pg_restore'):
            code=entry.replace('bridge.run(sys.argv[1],sys.argv[2:],input_fd=0,output_fd=1)',
                               'bridge.run(%r,sys.argv[1:],input_fd=0,output_fd=1)' % program)
            wrapper=wrappers/program
            wrapper.write_text('#!'+sys.executable+' -I\n'+code)
            wrapper.chmod(0o700)
        age=wrappers/'age'
        age.write_text('#!'+sys.executable+" -I\nimport pathlib,sys\nsys.stdout.buffer.write(b'INERT_AGE:'+pathlib.Path(sys.argv[-1]).read_bytes())\n")
        age.chmod(0o700)
        recipient=self.root/'recipients';recipient.write_text('age1'+'q'*58);recipient.chmod(0o600)
        entrypoint=ROOT/'scripts/lib/backup_recovery.py'
        result=subprocess.run([sys.executable,'-I',str(entrypoint),'backup','--environment','qa',
            '--output-dir',str(self.work),'--recipient-file',str(recipient),'--verify-table','platform.workspaces'],
            env={**self.env,'PATH':str(wrappers)+':/usr/bin:/bin','PGHOST':'supabase-db','PGPORT':'5432',
                 'PGDATABASE':'postgres','PGUSER':'postgres'},stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        record=json.loads(result.stdout)
        self.assertEqual(record['environment'],'qa')
        self.assertFalse(record['uploaded'])
        self.assertEqual(set(path.name for path in (self.work/record['job']).iterdir()),{'backup.tar.age','record.json'})
        self.assertNotIn(b'inert-private-db-password',result.stdout+result.stderr)


if __name__=='__main__': unittest.main()
