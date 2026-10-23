#!/usr/bin/env python3
"""Run only original Authz/CMS seed APIs in admitted immutable source images.

The privileged caller owns fresh approval, host locking, backup/migration ledger,
provider seeds and product/catalog acceptance. This phase grants none of them.
Database/migration/query adapters are trusted integration boundaries, never request
fields. Child logs are disabled and only bounded numeric evidence is returned.
"""
import copy
import grp
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid
try:
    from . import environment_inputs, host_compose, migration_inventory, migration_transport, release_manifest as records
except ImportError:
    import environment_inputs, host_compose, migration_inventory, migration_transport, release_manifest as records

ERROR='Hosted seed execution refused'
SEED_UID=0
CID=re.compile(r'[a-f0-9]{64}')
DB_URL=re.compile(r'postgres://postgres:[a-f0-9]{64}@supabase-db:5432/postgres')
EXPECTED=(('xynes-authz-service','src/db/seed/authz.seed.ts','seedAuthz','after-authz-schema-before-apps'),
          ('xynes-cms-core','src/infra/db/seeders.ts','seedAllDefaultTemplates','after-cms-schema-no-workspace-required'),
          ('xynes-cms-core','src/infra/db/seeders.ts','seedAllDefaultContentTypes','existing-workspaces-only'))
PEER_INSPECT=('{"id":{{json .Id}},"image":{{json .Config.Image}},"labels":{{json .Config.Labels}},'
              '"state":{{json .State.Status}},"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},'
              '"networks":{{json .NetworkSettings.Networks}}}')
SEED_INSPECT=('{"id":{{json .Id}},"image":{{json .Config.Image}},"labels":{{json .Config.Labels}},'
              '"user":{{json .Config.User}},"readonly":{{json .HostConfig.ReadonlyRootfs}},'
              '"cap_drop":{{json .HostConfig.CapDrop}},"security_opt":{{json .HostConfig.SecurityOpt}},'
              '"networks":{{json .NetworkSettings.Networks}},"state":{{json .State.Status}},"exit_code":{{json .State.ExitCode}}}')
IMAGE_INSPECT='{"os":{{json .Os}},"architecture":{{json .Architecture}},"labels":{{json .Config.Labels}}}'
POSTCONDITIONS_SQL="""SELECT json_build_object(
 'authz_permissions',(SELECT count(*) FROM authz.permissions),
 'authz_roles',(SELECT count(*) FROM authz.roles),
 'authz_mappings',(SELECT count(*) FROM authz.role_permissions),
 'cms_templates',(SELECT count(*) FROM cms.global_content_templates WHERE key IN ('blog_post','program','event')),
 'workspaces',(SELECT count(*) FROM platform.workspaces),
 'cms_defaults_satisfied',(SELECT count(*) FROM platform.workspaces w
   CROSS JOIN (VALUES ('blog-post','blog'),('program','programs'),('event','events')) AS d(slug,route)
   WHERE EXISTS(SELECT 1 FROM cms.content_types t WHERE t.workspace_id=w.id AND (t.slug=d.slug OR t.route_segment=d.route)))
);"""
PREAMBLE='''import postgres from "postgres";
import {drizzle} from "drizzle-orm/postgres-js";
import {sql} from "drizzle-orm";
const emit=console.log.bind(console);
console.log=console.warn=console.error=()=>{};
const client=postgres(process.env.DATABASE_URL,{max:1,connect_timeout:10,idle_timeout:10,prepare:false});
'''
AUTHZ_CODE=PREAMBLE+'''import * as schema from "/app/src/db/schema.ts";
import {seedAuthz,AUTHZ_PERMISSIONS,AUTHZ_ROLES} from "/app/src/db/seed/authz.seed.ts";
try {
 const permissions=AUTHZ_PERMISSIONS.length,roles=AUTHZ_ROLES.length;
 const mappings=AUTHZ_ROLES.reduce((n,role)=>n+new Set(role.permissions).size,0);
 if(permissions<1||permissions>2048||roles<1||roles>128||mappings<1||mappings>65536)throw new Error("seed catalog refused");
 const database=drizzle(client,{schema});
 await database.transaction(async db=>{
  await db.execute(sql`SET LOCAL lock_timeout='5s'`);await db.execute(sql`SET LOCAL statement_timeout='120s'`);
  await seedAuthz({db});
 });
 emit(JSON.stringify({kind:"authz",permissions,roles,mappings}));
} catch {process.exitCode=1;} finally {try{await client.end({timeout:5});}catch{process.exitCode=1;}}
'''
CMS_CODE=PREAMBLE+'''import * as schema from "/app/src/infra/db/schema.ts";
import {seedAllDefaultTemplates,seedAllDefaultContentTypes,DEFAULT_TEMPLATE_DEFINITIONS,DEFAULT_CONTENT_TYPE_DEFINITIONS} from "/app/src/infra/db/seeders.ts";
try {
 if(DEFAULT_TEMPLATE_DEFINITIONS.length!==3||DEFAULT_CONTENT_TYPE_DEFINITIONS.length!==3)throw new Error("seed catalog refused");
 let workspaces=0;
 const database=drizzle(client,{schema});
 await database.transaction(async db=>{
  await db.execute(sql`SET LOCAL lock_timeout='5s'`);await db.execute(sql`SET LOCAL statement_timeout='120s'`);
  const rows=await db.execute(sql`SELECT id FROM platform.workspaces ORDER BY id LIMIT 10001`);
  if(rows.length>10000)throw new Error("seed workspace limit");workspaces=rows.length;
  await seedAllDefaultTemplates(db);
  for(const row of rows)await seedAllDefaultContentTypes(db,row.id);
 });
 emit(JSON.stringify({kind:"cms",templates:3,workspaces,defaults:workspaces*3}));
} catch {process.exitCode=1;} finally {try{await client.end({timeout:5});}catch{process.exitCode=1;}}
'''

class SeedError(ValueError):pass

def require(value):
    if not value:raise SeedError(ERROR)

def read_database(stage):
    host_compose.stage_environment(stage)
    raw=host_compose.private_environment(Path('/srv/xynes')/stage/'env'/('.env.'+stage),uid=0,gid=grp.getgrnam('xynes').gr_gid)
    return environment_inputs.parse_existing(raw,stage)['DATABASE_URL']

def private_work(path):
    require(os.getuid()==SEED_UID and isinstance(path,Path) and path.is_absolute() and path.resolve(strict=True)==path)
    info=path.lstat();require(stat.S_ISDIR(info.st_mode) and info.st_uid==SEED_UID and stat.S_IMODE(info.st_mode)==0o700)

def requirements(verified):
    require(type(verified) is migration_transport.Verified and verified.deployable is False)
    records.validate_controller(verified.controller)
    plan=verified.plan;require(plan['source_manifest_sha256']==verified.source_manifest_sha256)
    seeds=plan['seed_requirements'];require(type(seeds) is list and len(seeds)==3)
    for row,(owner,path,function,condition) in zip(seeds,EXPECTED):
        require(type(row) is dict and set(row)=={'owner','path','function','condition','source_commit','bindings'}
                and (row['owner'],row['path'],row['function'],row['condition'])==(owner,path,function,condition)
                and row['source_commit']==plan['source_commits'][owner]==verified.inventory['source_commits'][owner])
        lookup={binding['path']:binding for binding in verified.inventory['migrations'][owner]}
        require(row['bindings']==[lookup[name] for name in sorted(migration_inventory.PREREQUISITES[owner])])
    return copy.deepcopy(seeds)

def network_ownership(labels,stage,name):
    require(type(labels) is dict and all(type(key) is str and type(value) is str for key,value in labels.items()))
    # Preserve the ownership contracts of the existing QA/prod networks and the
    # separately provisioned staging substrate. Never recreate a data network
    # merely to change its labels or accept a managed marker alone.
    staging={'io.xynes.managed':'true','io.xynes.owner':'xynes-infra',
             'io.xynes.environment':'staging','io.xynes.apex.substrate':'1'}
    canonical=stage=='staging' and labels==staging
    managed=(not any(key.startswith('io.xynes.') for key in labels)
             and labels.get('com.xynes.managed')=='true'
             and labels.get('com.xynes.environment',stage)==stage
             and labels.get('com.xynes.network',name)==name
             and labels.get('com.xynes.owner','xynes-infra')=='xynes-infra'
             and (labels.get('com.xynes.environment')==stage
                  or (stage in ('qa','prod') and labels.get('com.xynes.network')==name)))
    require(canonical or managed)

class Driver:
    def __init__(self,compose,*,verify_counts,database_loader=read_database,migration_validator=migration_transport.application_inputs):
        require(isinstance(compose,host_compose.Driver) and all(callable(f) for f in (verify_counts,database_loader,migration_validator)))
        self.compose=compose;self.verify_counts=verify_counts;self.database_loader=database_loader;self.migration_validator=migration_validator

    def call(self,args,timeout=30):return self.compose.call(args,timeout=timeout,limit=65536)

    def network(self,config,prepared):
        name=config['networks']['environment']['name']
        require(name==f'xynes_{self.compose.stage}_net')
        network=records.strict_json(self.call(['network','inspect','--format','{{json .}}',name]).decode())
        require(type(network) is dict and CID.fullmatch(network.get('Id','')) and network.get('Name')==name
                and network.get('Driver')=='bridge' and network.get('Scope')=='local'
                and network.get('Internal') is False and network.get('Ingress') is False
                and type(network.get('Containers')) is dict and 0<len(network['Containers'])<=32)
        network_ownership(network.get('Labels'),self.compose.stage,name)
        candidates=[]
        for cid in network['Containers']:
            require(type(cid) is str and CID.fullmatch(cid))
            row=records.strict_json(self.call(['inspect','--format',PEER_INSPECT,cid]).decode())
            require(type(row) is dict and row.get('id')==cid and type(row.get('networks')) is dict)
            attachment=row['networks'].get(name)
            require(type(attachment) is dict and attachment.get('NetworkID')==network['Id'])
            aliases=attachment.get('Aliases') or []
            dns_names=attachment.get('DNSNames') or []
            require(type(aliases) is list and type(dns_names) is list and len(aliases)<=32 and len(dns_names)<=32
                    and all(type(alias) is str for alias in aliases+dns_names))
            endpoint=network['Containers'][cid]
            require(type(endpoint) is dict and type(endpoint.get('Name')) is str)
            resolved_names=set(aliases+dns_names+[endpoint['Name']])
            labels=row.get('labels') or {}
            if 'supabase-db' in resolved_names or labels.get('com.docker.compose.service')=='supabase-db':
                require('supabase-db' in aliases and row.get('image')==prepared.images['IMAGE_SUPABASE_DB']
                        and labels.get('com.xynes.managed')=='true' and labels.get('com.xynes.environment')==self.compose.stage
                        and labels.get('com.docker.compose.project')=='xynes-'+self.compose.stage
                        and labels.get('com.docker.compose.service')=='supabase-db'
                        and labels.get('com.docker.compose.oneoff')=='False'
                        and row.get('state')=='running' and row.get('health')=='healthy')
                candidates.append(cid)
        require(len(candidates)==1)
        return name,network['Id'],candidates[0]

    def source_image(self,image,owner,verified):
        self.call(['pull','--platform','linux/amd64',image],timeout=180)
        result=records.strict_json(self.call(['image','inspect','--format',IMAGE_INSPECT,image]).decode())
        require(type(result) is dict and result.get('os')=='linux' and result.get('architecture')=='amd64'
                and type(result.get('labels')) is dict)
        labels=result['labels'];controller=verified.controller
        require(labels.get('org.opencontainers.image.revision')==verified.plan['source_commits'][owner]
                and labels.get('com.xynes.apex.controller-commit')==controller['commit']
                and labels.get('com.xynes.apex.run-id')==str(controller['run_id'])
                and labels.get('com.xynes.apex.run-attempt')==str(controller['run_attempt']))

    def owned(self,cid,image,labels,network):
        row=records.strict_json(self.call(['inspect','--format',SEED_INSPECT,cid]).decode())
        require(type(row) is dict and row.get('id')==cid and row.get('image')==image and type(row.get('labels')) is dict
                and all(row['labels'].get(key)==value for key,value in labels.items())
                and row.get('user')=='1001:1001' and row.get('readonly') is True
                and row.get('cap_drop')==['ALL'] and row.get('security_opt')==['no-new-privileges:true']
                and type(row.get('networks')) is dict and set(row['networks'])=={network[0]}
                and row['networks'][network[0]].get('NetworkID')==network[1])
        return row

    def oneoff(self,kind,image,code,file,network,controller):
        nonce=uuid.uuid4().hex;cid=None;cleanup_failed=False;attempted=False
        name='apex-seed-'+self.compose.stage+'-'+kind+'-'+nonce
        labels={'com.xynes.managed':'true','com.xynes.environment':self.compose.stage,
                'com.xynes.apex.oneoff':'true','com.xynes.apex.seed':kind,'com.xynes.apex.seed-nonce':nonce,
                'com.xynes.apex.controller-commit':controller['commit']}
        try:
            args=['create','--name',name,
                  '--platform','linux/amd64','--network',network[1], '--read-only','--cap-drop','ALL',
                  '--security-opt','no-new-privileges:true','--user','1001:1001','--memory','256m','--pids-limit','128',
                  '--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=16m,uid=1001,gid=1001,mode=0700',
                  '--no-healthcheck','--log-driver','none','--env-file',str(file),'--workdir','/app']
            for key,value in labels.items():args+=['--label',key+'='+value]
            args+=['--entrypoint','/usr/local/bin/bun',image,'--eval',code]
            attempted=True
            raw=self.call(args);require(type(raw) is bytes and CID.fullmatch(raw.decode('ascii').strip()))
            cid=raw.decode('ascii').strip();self.owned(cid,image,labels,network)
            result=records.strict_json(self.call(['start','--attach',cid],timeout=240).decode())
            row=self.owned(cid,image,labels,network)
            require(row.get('state')=='exited' and type(row.get('exit_code')) is int and row['exit_code']==0)
            expected={'kind','permissions','roles','mappings'} if kind=='authz' else {'kind','templates','workspaces','defaults'}
            require(type(result) is dict and set(result)==expected and result['kind']==kind
                    and all(type(value) is int and 0<=value<=65536 for key,value in result.items() if key!='kind'))
            if kind=='authz':require(0<result['permissions']<=2048 and 0<result['roles']<=128 and result['mappings']>0)
            else:require(result['templates']==3 and result['workspaces']<=10000 and result['defaults']==result['workspaces']*3)
            return result
        finally:
            if cid is None and attempted:
                # A timed-out client may have created its container before losing
                # the reply. Name lookup alone never authorizes removal: recover
                # only one exact CID and inspect the unpredictable ownership nonce.
                try:
                    found=self.call(['ps','--all','--filter','name=^/'+name+'$','--format','{{.ID}}','--no-trunc']).decode('ascii').splitlines()
                    require(len(found)<=1 and all(CID.fullmatch(value) for value in found))
                    if found:
                        self.owned(found[0],image,labels,network);cid=found[0]
                except Exception:cleanup_failed=True
            if cid is not None:
                try:
                    self.owned(cid,image,labels,network);self.call(['rm','--force',cid])
                except Exception:cleanup_failed=True
            if cleanup_failed:raise SeedError(ERROR) from None

    def run(self,prepared,verified,*,work):
        file=None;before=None
        try:
            private_work(work);self.migration_validator(verified);seeds=requirements(verified)
            config=self.compose.config(prepared)
            database=self.database_loader(self.compose.stage)
            require(type(database) is str and DB_URL.fullmatch(database)
                    and all(config['services'][service]['environment']['DATABASE_URL']==database for service in ('authz-service','cms-core')))
            network=self.network(config,prepared)
            descriptor,name=tempfile.mkstemp(prefix='seed-db-',suffix='.env',dir=work);file=Path(name)
            with os.fdopen(descriptor,'wb') as out:
                os.fchmod(out.fileno(),0o600);out.write(('DATABASE_URL='+database+'\n').encode());out.flush();os.fsync(out.fileno())
                before=os.fstat(out.fileno())
            results={}
            for kind,owner,key,code in [('authz','xynes-authz-service','IMAGE_AUTHZ',AUTHZ_CODE),('cms','xynes-cms-core','IMAGE_CMS',CMS_CODE)]:
                image=prepared.images[key];self.source_image(image,owner,verified)
                require(self.network(config,prepared)==network)
                results[kind]=self.oneoff(kind,image,code,file,network,verified.controller)
            require(self.network(config,prepared)==network)
            counts=self.verify_counts(self.compose.stage,POSTCONDITIONS_SQL)
            require(type(counts) is dict and set(counts)=={'authz_permissions','authz_roles','authz_mappings','cms_templates','workspaces','cms_defaults_satisfied'}
                    and all(type(value) is int and 0<=value<=10000000 for value in counts.values())
                    and counts['authz_permissions']>=results['authz']['permissions']
                    and counts['authz_roles']>=results['authz']['roles']
                    and counts['authz_mappings']>=results['authz']['mappings']
                    and counts['cms_templates']==results['cms']['templates']==3
                    and counts['workspaces']==results['cms']['workspaces']
                    and counts['cms_defaults_satisfied']==results['cms']['defaults'])
            return {'stage':self.compose.stage,'seeds_applied':True,'seed_requirements':seeds,
                    'counts':copy.deepcopy(counts),'stage_approved':False,'product_accepted':False}
        except Exception:raise SeedError(ERROR) from None
        finally:
            if file is not None:
                try:
                    require(before is not None and os.path.samestat(before,file.lstat()));file.unlink()
                except Exception:raise SeedError(ERROR) from None
