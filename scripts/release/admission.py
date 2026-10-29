#!/usr/bin/env python3
"""Read-only native admission evidence; callers must recheck immediately before write.

REST approval history has no attempt/timestamp binding: reruns are refused. Its
jobs API has no environment field, so an injected trusted YAML parser reads the
hash-verified exact-commit workflow. No token, network, publication or host code.
Source-image qualification remains the responsibility of source_checks and
release_manifest; this receipt alone never authorizes an image or deployment.
"""
import base64
import datetime
import hashlib
import re

try:
    from . import release_manifest
except ImportError:
    import release_manifest

REPOSITORY = 'Xynes-Studio/xynes-infra'
REPOSITORY_ID = 1415400917
REVIEWER = 'xynes-admin'
WORKFLOW = '.github/workflows/apex-build.yml'
BUILD_GUARD = "github.repository == 'Xynes-Studio/xynes-infra' && github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main' && github.ref_protected"
PREFIX = '/repos/' + REPOSITORY
CHECKS = ('lint', 'test', 'coverage', 'build', 'typecheck', 'security')
NO_BYPASS_QUERY = ('query ApexMainNoBypass { repository(owner:"Xynes-Studio",name:"xynes-infra",followRenames:false) {'
    ' databaseId nameWithOwner ref(qualifiedName:"refs/heads/main") { name prefix target { oid }'
    ' branchProtectionRule { pattern bypassPullRequestAllowances(first:1) {'
    ' totalCount nodes { id } pageInfo { hasNextPage } } } } } }')


class AdmissionError(ValueError):
    """Refusals contain no remote response, source data or credential."""


def require(value):
    if not value:
        raise AdmissionError('Native release admission refused')


def positive(value):
    return type(value) is int and 0 < value <= 9007199254740991


def repository(value):
    require(isinstance(value, dict) and value.get('id') == REPOSITORY_ID
            and value.get('full_name') == REPOSITORY)


def user(value):
    require(isinstance(value, dict) and positive(value.get('id'))
            and value.get('type') == 'User' and isinstance(value.get('login'), str))
    return value['id']


def no_bypass(read_graphql, sha):
    require(callable(read_graphql))
    proof = read_graphql(NO_BYPASS_QUERY)
    require(isinstance(proof, dict) and not proof.get('errors'))
    repo = proof['data']['repository']
    require(repo['databaseId'] == REPOSITORY_ID and repo['nameWithOwner'] == REPOSITORY)
    ref = repo['ref']
    require(ref['name'] == 'main' and ref['prefix'] == 'refs/heads/' and ref['target']['oid'] == sha)
    rule = ref['branchProtectionRule']
    require(rule['pattern'] == 'main')
    connection = rule['bypassPullRequestAllowances']
    require(type(connection['totalCount']) is int and connection['totalCount'] == 0
            and connection['nodes'] == [] and connection['pageInfo']['hasNextPage'] is False)


def protected_main(value, sha, read_graphql):
    require(isinstance(value, dict))
    required = value.get('required_status_checks', {})
    require(isinstance(required, dict) and required.get('strict') is True)
    checks = required.get('checks')
    require(isinstance(checks, list) and len(checks) == len(CHECKS)
            and {c.get('context') for c in checks if isinstance(c, dict)} == set(CHECKS)
            and all(isinstance(c, dict) and positive(c.get('app_id')) and c['app_id'] == 15368 for c in checks))
    review = value.get('required_pull_request_reviews', {})
    require(isinstance(review, dict) and all(review.get(key) is True for key in
            ('dismiss_stale_reviews', 'require_code_owner_reviews', 'require_last_push_approval'))
            and positive(review.get('required_approving_review_count')))
    if 'bypass_pull_request_allowances' in review:
        bypass = review['bypass_pull_request_allowances']
        require(isinstance(bypass, dict) and all(bypass.get(k) == [] for k in ('users', 'teams', 'apps')))
    else:
        # REST omits empty allowances; require affirmative native connection evidence.
        no_bypass(read_graphql, sha)
    for name, enabled in [('enforce_admins', True), ('required_conversation_resolution', True),
                          ('allow_force_pushes', False), ('allow_deletions', False)]:
        require(isinstance(value.get(name), dict) and value[name].get('enabled') is enabled)


def workflow_contents(value, decode):
    require(isinstance(value, dict) and value.get('type') == 'file'
            and value.get('path') == WORKFLOW and value.get('encoding') == 'base64'
            and isinstance(value.get('content'), str) and positive(value.get('size'))
            and value['size'] <= 256 * 1024 and isinstance(value.get('sha'), str))
    encoded = value['content'].replace('\n', '')
    require(len(encoded) <= 350000)
    raw = base64.b64decode(encoded, validate=True)
    require(len(raw) == value['size'] and base64.b64encode(raw).decode() == encoded
            and hashlib.sha1(f'blob {len(raw)}\0'.encode() + raw).hexdigest() == value['sha'])
    workflow = decode(raw)
    require(isinstance(workflow, dict) and workflow.get('on') == {'workflow_dispatch': {}}
            and isinstance(workflow.get('jobs'), dict))
    job = workflow['jobs'].get('publish-images')
    require(isinstance(job, dict) and job.get('name', 'publish-images') == 'publish-images'
            and job.get('environment') == 'release' and 'uses' not in job
            and job.get('needs') == ['admission', 'build-images', 'product-smoke']
            and job.get('permissions') == {'contents': 'read', 'actions': 'read', 'packages': 'write'})
    smoke = workflow['jobs'].get('product-smoke')
    require(isinstance(smoke, dict) and smoke.get('name') == 'product-smoke'
            and smoke.get('needs') == ['admission', 'build-images'] and smoke.get('if') == BUILD_GUARD
            and smoke.get('permissions') == {'contents': 'read', 'actions': 'read'}
            and 'environment' not in smoke and 'uses' not in smoke and 'continue-on-error' not in smoke)
    return value['sha']


def verify_checks(get, sha):
    checks, ids, total = [], set(), None
    for page in range(1, 21):
        response = get(f'{PREFIX}/commits/{sha}/check-runs?filter=all&per_page=100&page={page}')
        require(isinstance(response, dict) and isinstance(response.get('check_runs'), list)
                and len(response['check_runs']) <= 100 and type(response.get('total_count')) is int
                and 0 <= response['total_count'] <= 2000)
        if total is None:
            total = response['total_count']
        require(response['total_count'] == total)
        for check in response['check_runs']:
            require(isinstance(check, dict) and positive(check.get('id')) and check['id'] not in ids)
            ids.add(check['id']); checks.append(check)
        if len(response['check_runs']) < 100:
            break
    else:
        require(False)
    require(len(checks) == total)
    receipts = []
    for name in CHECKS:
        matching = [c for c in checks if c.get('name') == name]
        require(matching)
        chosen = max(matching, key=lambda c: c['id'])
        require(chosen.get('head_sha') == sha and positive(chosen.get('app', {}).get('id'))
                and chosen['app']['id'] == 15368
                and chosen.get('status') == 'completed' and chosen.get('conclusion') == 'success')
        link = chosen.get('details_url')
        require(isinstance(link, str))
        match = re.fullmatch(r'https://github\.com/' + re.escape(REPOSITORY)
                            + r'/actions/runs/([1-9][0-9]*)/job/([1-9][0-9]*)', link)
        require(match)
        run_id, job_id = map(int, match.groups())
        require(positive(run_id) and positive(job_id))
        job = get(f'{PREFIX}/actions/jobs/{job_id}')
        require(job.get('id') == job_id and job.get('name') == name and job.get('head_sha') == sha
                and job.get('run_id') == run_id and positive(job.get('run_attempt'))
                and job.get('status') == 'completed' and job.get('conclusion') == 'success'
                and job.get('check_run_url') == f'https://api.github.com{PREFIX}/check-runs/{chosen["id"]}')
        run = get(f'{PREFIX}/actions/runs/{run_id}')
        repository(run.get('repository')); repository(run.get('head_repository'))
        path = '.github/workflows/' + ('security.yml' if name == 'security' else 'quality-gates.yml')
        require(run.get('id') == run_id and run.get('head_sha') == sha and run.get('path') == path
                and run.get('run_attempt') == job['run_attempt'] and positive(run.get('workflow_id'))
                and run.get('check_suite_id') == chosen.get('check_suite', {}).get('id')
                and positive(run.get('check_suite_id')) and run.get('status') == 'completed'
                and run.get('conclusion') == 'success')
        workflow = get(f'{PREFIX}/actions/workflows/{run["workflow_id"]}')
        require(workflow.get('id') == run['workflow_id'] and workflow.get('path') == path
                and workflow.get('state') == 'active')
        receipts.append({'name': name, 'check_id': chosen['id'], 'run_id': run_id, 'job_id': job_id,
                         'run_attempt': job['run_attempt'], 'workflow_id': run['workflow_id']})
    return receipts


def verify_publisher_job(get, job_id, run_id, sha):
    job = get(f'{PREFIX}/actions/jobs/{job_id}')
    require(job.get('id') == job_id and job.get('run_id') == run_id
            and type(job.get('run_attempt')) is int and job['run_attempt'] == 1
            and job.get('head_sha') == sha and job.get('head_branch') == 'main'
            and job.get('name') == 'publish-images' and job.get('status') == 'in_progress'
            and job.get('conclusion') is None and job.get('run_url') == f'https://api.github.com{PREFIX}/actions/runs/{run_id}')


def verify_release_approval(get, run_id, initiators):
    reviewer = get('/users/' + REVIEWER); reviewer_id = user(reviewer)
    require(reviewer['login'].casefold() == REVIEWER.casefold() and reviewer_id not in initiators)
    env = get(PREFIX + '/environments/release')
    require(positive(env.get('id')) and env.get('name') == 'release' and env.get('can_admins_bypass') is False
            and env.get('url') == 'https://api.github.com' + PREFIX + '/environments/release'
            and env.get('deployment_branch_policy') == {'protected_branches': False, 'custom_branch_policies': True})
    rules = env.get('protection_rules'); require(isinstance(rules, list) and len(rules) <= 20)
    reviews = [r for r in rules if isinstance(r, dict) and r.get('type') == 'required_reviewers']
    require(len(reviews) == 1 and reviews[0].get('prevent_self_review') is True)
    reviewers = reviews[0].get('reviewers')
    require(isinstance(reviewers, list) and len(reviewers) == 1 and reviewers[0].get('type') == 'User'
            and user(reviewers[0].get('reviewer')) == reviewer_id)
    policies = get(PREFIX + '/environments/release/deployment-branch-policies?per_page=100&page=1')
    require(policies.get('total_count') == 1 and isinstance(policies.get('branch_policies'), list)
            and len(policies['branch_policies']) == 1 and policies['branch_policies'][0].get('name') == 'main'
            and policies['branch_policies'][0].get('type') == 'branch')
    approvals = get(f'{PREFIX}/actions/runs/{run_id}/approvals')
    require(isinstance(approvals, list) and len(approvals) <= 100)
    matching = [a for a in approvals if isinstance(a, dict) and isinstance(a.get('environments'), list)
                and any(isinstance(e, dict) and e.get('id') == env['id'] for e in a['environments'])]
    require(len(matching) == 1 and matching[0].get('state') == 'approved'
            and user(matching[0].get('user')) == reviewer_id
            and matching[0]['user']['login'].casefold() == REVIEWER.casefold())
    require(all(e.get('name') == 'release' and e.get('url') == env['url']
                for e in matching[0]['environments'] if isinstance(e, dict) and e.get('id') == env['id']))
    return env['id'], reviewer_id


def verify_source_approval(get, sha, initiators):
    """Bind the merged final PR and its exact independently reviewed tree.

    The URL/status in a manifest is data, never approval. Merge and squash are
    supported by comparing the complete immutable Git trees, not parent shape.
    """
    try:
        require(callable(get) and isinstance(sha,str) and release_manifest.SHA.fullmatch(sha)
                and type(initiators) is set and all(positive(v) for v in initiators))
        path='infra/release/source-manifest.json'
        def manifest_at(commit):
            value=get(f'{PREFIX}/contents/{path}?ref={commit}')
            require(isinstance(value,dict) and value.get('type')=='file' and value.get('path')==path
                    and value.get('encoding')=='base64' and type(value.get('content')) is str
                    and type(value.get('size')) is int and 0<value['size']<=release_manifest.MAX_BYTES
                    and len(value['content'])<=350000)
            encoded=value['content'].replace('\n','');raw=base64.b64decode(encoded,validate=True)
            require(len(raw)==value['size'] and base64.b64encode(raw).decode()==encoded
                    and hashlib.sha1(f'blob {len(raw)}\0'.encode()+raw).hexdigest()==value.get('sha'))
            result=release_manifest.strict_json(raw)
            release_manifest.source_context.validate_manifest(result,'release')
            return raw,result
        raw,manifest=manifest_at(sha)
        match=re.fullmatch(r'https://github\.com/Xynes-Studio/xynes-infra/pull/([1-9][0-9]*)',manifest['approval_reference'])
        require(match is not None);number=int(match[1]);require(positive(number))
        reviewer=get('/users/'+REVIEWER);reviewer_id=user(reviewer)
        require(reviewer['login'].casefold()==REVIEWER.casefold() and reviewer_id not in initiators)
        pull_path=f'{PREFIX}/pulls/{number}'
        def pull():
            value=get(pull_path);require(isinstance(value,dict))
            require(value.get('number')==number and value.get('state')=='closed' and value.get('merged') is True
                    and value.get('draft') is False and value.get('merge_commit_sha')==sha
                    and value.get('html_url')==manifest['approval_reference'])
            base,head=value.get('base'),value.get('head')
            require(isinstance(base,dict) and isinstance(head,dict) and base.get('ref')=='main')
            repository(base.get('repo'));repository(head.get('repo'))
            require(isinstance(head.get('sha'),str) and release_manifest.SHA.fullmatch(head['sha']))
            author=user(value.get('user'));require(author!=reviewer_id)
            merged=value.get('merged_at');require(type(merged) is str and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z',merged))
            datetime.datetime.fromisoformat(merged.replace('Z','+00:00'))
            return {'head':head['sha'],'author':author,'merged_at':merged,'merge':sha}
        initial=pull()
        def reviews():
            rows=[];seen=set()
            for page in range(1,21):
                values=get(pull_path+f'/reviews?per_page=100&page={page}')
                require(type(values) is list and len(values)<=100)
                for row in values:
                    require(type(row) is dict and positive(row.get('id')) and row['id'] not in seen
                            and type(row.get('user')) is dict and positive(row['user'].get('id'))
                            and row.get('state') in ('APPROVED','CHANGES_REQUESTED','DISMISSED','COMMENTED','PENDING'))
                    seen.add(row['id']);rows.append(row)
                if len(values)<100:break
            else:require(False)
            selected=[row for row in rows if row['user']['id']==reviewer_id
                      and row['state'] in ('APPROVED','CHANGES_REQUESTED','DISMISSED')]
            require(selected);chosen=max(selected,key=lambda row:row['id'])
            require(chosen['state']=='APPROVED' and user(chosen['user'])==reviewer_id
                    and chosen['user']['login'].casefold()==REVIEWER.casefold()
                    and chosen.get('commit_id')==initial['head'])
            submitted=chosen.get('submitted_at')
            require(type(submitted) is str and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z',submitted)
                    and datetime.datetime.fromisoformat(submitted.replace('Z','+00:00'))
                    <=datetime.datetime.fromisoformat(initial['merged_at'].replace('Z','+00:00')))
            return {'id':chosen['id'],'commit':chosen['commit_id'],'submitted_at':submitted}
        review=reviews()
        def tree(commit):
            value=get(f'{PREFIX}/git/commits/{commit}')
            require(type(value) is dict and value.get('sha')==commit and type(value.get('tree')) is dict
                    and isinstance(value['tree'].get('sha'),str) and release_manifest.SHA.fullmatch(value['tree']['sha']))
            return value['tree']['sha']
        head_tree,merge_tree=tree(initial['head']),tree(sha)
        require(head_tree==merge_tree and manifest_at(initial['head'])[0]==raw)
        # The reads above may span multiple pages. Refresh the mutable decision
        # and final PR head before returning any write-boundary proof.
        require(pull()==initial and reviews()==review and manifest_at(sha)[0]==raw)
        branch=get(PREFIX+'/branches/main')
        require(type(branch) is dict and branch.get('name')=='main' and branch.get('protected') is True
                and type(branch.get('commit')) is dict and branch['commit'].get('sha')==sha)
        return {'approval_reference':manifest['approval_reference'],'pull_number':number,
                'head_commit':initial['head'],'merge_commit':sha,'tree_sha':merge_tree,
                'reviewer_id':reviewer_id,'review_id':review['id'],
                'source_manifest_sha256':release_manifest.canonical_sha256(manifest)}
    except Exception:
        raise AdmissionError('Native source approval refused') from None


def validate_admission(controller, job_id, read, parse_workflow, read_graphql=None):
    """Injected read(path) returns a trusted, complete GitHub GET response."""
    try:
        release_manifest.validate_controller(controller)
        require(controller['run_attempt'] == 1 and positive(job_id)
                and callable(read) and callable(parse_workflow))
        sha, run_id = controller['commit'], controller['run_id']
        def get(path):
            value = read(path)
            require(isinstance(value, (dict, list)))
            return value
        repo = get(PREFIX); repository(repo)
        require(repo.get('private') is False and repo.get('fork') is False and repo.get('default_branch') == 'main')
        branch = get(PREFIX + '/branches/main')
        require(branch.get('name') == 'main' and branch.get('protected') is True
                and branch.get('commit', {}).get('sha') == sha)
        protected_main(get(PREFIX + '/branches/main/protection'), sha, read_graphql)
        run = get(f'{PREFIX}/actions/runs/{run_id}')
        repository(run.get('repository')); repository(run.get('head_repository'))
        require(run.get('id') == run_id and type(run.get('run_attempt')) is int
                and run['run_attempt'] == 1 and run.get('head_sha') == sha
                and run.get('head_branch') == 'main' and run.get('event') == 'workflow_dispatch'
                and run.get('path') == WORKFLOW and positive(run.get('workflow_id'))
                and run.get('status') == 'in_progress' and run.get('conclusion') is None)
        initiators = {user(run.get('actor')), user(run.get('triggering_actor'))}
        workflow = get(f'{PREFIX}/actions/workflows/{run["workflow_id"]}')
        require(workflow.get('id') == run['workflow_id'] and workflow.get('path') == WORKFLOW
                and workflow.get('state') == 'active')
        blob = workflow_contents(get(f'{PREFIX}/contents/{WORKFLOW}?ref={sha}'), parse_workflow)
        verify_publisher_job(get, job_id, run_id, sha)
        environment_id, reviewer_id = verify_release_approval(get, run_id, initiators)
        checks = verify_checks(get, sha)
        source_approval=verify_source_approval(get,sha,initiators)
        # Ignore incidental progress fields; revalidate authorization identity and policy.
        again = get(f'{PREFIX}/actions/runs/{run_id}')
        keys = ('id', 'run_attempt', 'workflow_id', 'path', 'head_sha', 'head_branch', 'event', 'status', 'conclusion')
        require(all(again.get(key) == run.get(key) for key in keys))
        require(type(again.get('run_attempt')) is int)
        repository(again.get('repository')); repository(again.get('head_repository'))
        require(all(user(again.get(key)) == user(run.get(key)) for key in ('actor', 'triggering_actor')))
        current = get(PREFIX + '/branches/main')
        require(current.get('name') == 'main' and current.get('protected') is True
                and current.get('commit', {}).get('sha') == sha)
        protected_main(get(PREFIX + '/branches/main/protection'), sha, read_graphql)
        # Check-run and source qualification can take many reads. Refresh the
        # write boundary after both before authorizing registry mutations.
        verify_publisher_job(get, job_id, run_id, sha)
        require(verify_release_approval(get, run_id, initiators) == (environment_id, reviewer_id))
        result = {**controller, 'admitted': True, 'repository_id': REPOSITORY_ID, 'job_id': job_id,
                  'workflow_blob': blob, 'environment_id': environment_id, 'reviewer_id': reviewer_id,
                  'checks': checks, 'source_approval':source_approval}
        result['evidence_sha256'] = release_manifest.canonical_sha256(result)
        return result
    except Exception:
        raise AdmissionError('Native release admission refused') from None
