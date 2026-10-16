#!/usr/bin/env python3
"""Pure-renderer bounds: execution/admission is tested separately against PostgreSQL."""
import hashlib
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'release'))
import migration_sql
OWNER='xynes-infra'
PATH='supabase/migrations/20261012090000_gateway_route_body_limits.sql'
SQL=b'CREATE TABLE platform.fixture (id integer);'
def unit(data=SQL):return {'owner':OWNER,'path':PATH,'sha256':hashlib.sha256(data).hexdigest(),'source_commit':'a'*40}
class Tests(unittest.TestCase):
 def test_deterministic_complete_batch_with_bound_stage_database_and_units(self):
  left=migration_sql.render('qa','postgres',[unit()],{(OWNER,PATH):SQL})
  self.assertEqual(left,migration_sql.render('qa','postgres',[unit()],{(OWNER,PATH):SQL}))
  self.assertIn('pg_advisory_lock',left);self.assertIn(unit()['sha256'],left)
  self.assertIn('current_database()',left)
 def test_stage_database_path_and_source_binding_refusals(self):
  for stage,db in [('dev','postgres'),('qa','postgres; SELECT private'),('qa','')]:
   with self.assertRaises(ValueError):migration_sql.render(stage,db,[unit()],{(OWNER,PATH):SQL})
  for changed in [{'sha256':'0'*64},{'source_commit':'main'},{'path':'../private'},{'owner':'foreign'},{'approved':True}]:
   value={**unit(),**changed}
   with self.assertRaises(ValueError):migration_sql.render('qa','postgres',[value],{(value['owner'],value['path']):SQL})
 def test_no_empty_duplicate_partial_or_extra_batch_payload(self):
  for units,payloads in [([],{}),([unit(),unit()],{(OWNER,PATH):SQL}),([unit()],{}),([unit()],{(OWNER,PATH):SQL,('foreign','file'):b'other'})]:
   with self.assertRaises(ValueError):migration_sql.render('qa','postgres',units,payloads)
 def test_transaction_escape_psql_meta_and_unbounded_input_refused(self):
  for data in [b'COMMIT;',b'BEGIN;',b'ROLLBACK;',b'END;',b'\\connect production',b'SELECT 1;\n\\! private',b'CREATE INDEX CONCURRENTLY x ON y(id);',b'\x00',b'x'*(1024*1024+1)]:
   with self.assertRaises(ValueError):migration_sql.render('qa','postgres',[unit(data)],{(OWNER,PATH):data})
 def test_aggregate_sql_is_bounded_before_rendering(self):
  data=b'-- '+b'x'*(1024*1024-4)+b'\n';units=[];payloads={}
  for i in range(17):
   value={**unit(data),'path':f'supabase/migrations/{20000101000000+i}_fixture.sql'};units.append(value);payloads[(OWNER,value['path'])]=data
  with self.assertRaises(ValueError):migration_sql.render('qa','postgres',units,payloads)

 def test_regular_plpgsql_blocks_and_views_are_supported(self):
  data=b'DO $$\nBEGIN\n PERFORM 1;\nEND $$;\nCREATE VIEW fixture AS SELECT 1;'
  self.assertIn(data.decode(),migration_sql.render('staging','postgres',[unit(data)],{(OWNER,PATH):data}))
if __name__=='__main__':unittest.main()
