#!/usr/bin/env python3
"""Fresh native stage admission; no artifact, staging acceptance or host authority.

The caller supplies trusted bounded native GET/GraphQL readers and an owned
JSON-as-YAML parser. REST approvals have no retry-attempt binding, so only a
distinct first-attempt manual deployment run is supported. Invoke again at the
host write boundary; this proof is revocable and never establishes staging
acceptance, candidate signature trust, rollback readiness or deployability.
"""
import base64
import copy
import hashlib

try:
    from . import admission, release_manifest as records
except ImportError:
    import admission
    import release_manifest as records

ERROR = 'Native deployment admission refused'
WORKFLOW = '.github/workflows/apex-deploy.yml'
STAGES = ('qa', 'staging', 'prod')


class DeployAdmissionError(ValueError):
    """Diagnostics contain no rejected values, native response or credentials."""


def require(value):
    if not value:
        raise DeployAdmissionError(ERROR)


def guard(stage):
    require(type(stage) is str and stage in STAGES)
    return ("${{ github.repository == 'Xynes-Studio/xynes-infra' && github.ref == 'refs/heads/main' "
            "&& github.ref_protected && github.event_name == 'workflow_dispatch' && github.run_attempt == 1 "
            "&& inputs.stage == '" + stage + "' }}")


def repository(value):
    require(type(value) is dict and type(value.get('id')) is int)
    admission.repository(value)


def run(value, controller, deployment):
    require(type(value) is dict)
    repository(value.get('repository'))
    repository(value.get('head_repository'))
    expected = dict(id=deployment['run_id'], run_attempt=1, path=WORKFLOW,
        head_sha=controller['commit'], head_branch='main', event='workflow_dispatch',
        status='in_progress', conclusion=None)
    require(all(type(value.get(key)) is type(item) and value[key] == item for key,item in expected.items())
            and admission.positive(value.get('workflow_id')))
    return {**expected, 'workflow_id':value['workflow_id'],
            'actor':admission.user(value.get('actor')), 'triggering_actor':admission.user(value.get('triggering_actor'))}


def job(value, stage, controller, deployment):
    require(type(value) is dict)
    expected = dict(id=deployment['job_id'], run_id=deployment['run_id'], run_attempt=1,
        head_sha=controller['commit'], head_branch='main', name='deploy-'+stage,
        status='in_progress', conclusion=None,
        run_url='https://api.github.com'+admission.PREFIX+'/actions/runs/'+str(deployment['run_id']))
    require(all(type(value.get(key)) is type(item) and value[key] == item for key,item in expected.items()))


def workflow(value, stage, parse):
    require(type(value) is dict and value.get('type') == 'file' and value.get('path') == WORKFLOW
            and value.get('encoding') == 'base64' and type(value.get('content')) is str
            and type(value.get('size')) is int and 0 < value['size'] <= records.MAX_BYTES
            and type(value.get('sha')) is str and len(value['content']) <= 350000)
    encoded = value['content'].replace('\n','')
    raw = base64.b64decode(encoded,validate=True)
    require(len(raw) == value['size'] and base64.b64encode(raw).decode() == encoded
            and hashlib.sha1(f'blob {len(raw)}\0'.encode()+raw).hexdigest() == value['sha'])
    document = parse(raw)
    records.bounded_tree(document)
    require(type(document) is dict and type(document.get('on')) is dict
            and set(document['on']) == {'workflow_dispatch'}
            and type(document['on']['workflow_dispatch']) is dict and type(document.get('jobs')) is dict)
    selected = document['jobs'].get('deploy-'+stage)
    require(type(selected) is dict and selected.get('name','deploy-'+stage) == 'deploy-'+stage
            and selected.get('environment') == stage and 'uses' not in selected
            and selected.get('permissions') == {'contents':'read','actions':'read'}
            and selected.get('if') == guard(stage))
    return value['sha']


def environment(get, stage, deployment, initiators):
    path = admission.PREFIX+'/environments/'+stage
    env = get(path)
    require(type(env) is dict and admission.positive(env.get('id')) and env.get('name') == stage
            and env.get('url') == 'https://api.github.com'+path and env.get('can_admins_bypass') is False
            and env.get('deployment_branch_policy') == {'protected_branches':False,'custom_branch_policies':True})
    policies = get(path+'/deployment-branch-policies?per_page=100&page=1')
    require(type(policies) is dict and type(policies.get('total_count')) is int and policies['total_count'] == 1
            and type(policies.get('branch_policies')) is list and len(policies['branch_policies']) == 1
            and type(policies['branch_policies'][0]) is dict
            and policies['branch_policies'][0].get('name') == 'main' and policies['branch_policies'][0].get('type') == 'branch')
    rules = env.get('protection_rules')
    require(type(rules) is list and len(rules) <= 20
            and all(type(rule) is dict and admission.positive(rule.get('id'))
                    and rule.get('type') in ('required_reviewers','branch_policy') for rule in rules)
            and len({rule['id'] for rule in rules}) == len(rules)
            and len({rule['type'] for rule in rules}) == len(rules))
    review = [rule for rule in rules if rule['type'] == 'required_reviewers']
    reviewers = {}
    if review:
        require(review[0].get('prevent_self_review') is True
                and type(review[0].get('reviewers')) is list and 0 < len(review[0]['reviewers']) <= 6)
        for configured in review[0]['reviewers']:
            require(type(configured) is dict and configured.get('type') == 'User')
            user = configured.get('reviewer')
            identity = admission.user(user)
            require(identity not in reviewers and 0 < len(user['login']) <= 64)
            reviewers[identity] = user['login'].casefold()
    if stage == 'prod':
        aman = get('/users/'+admission.REVIEWER)
        identity = admission.user(aman)
        require(aman['login'].casefold() == admission.REVIEWER.casefold()
                and reviewers == {identity:admission.REVIEWER.casefold()} and identity not in initiators)
    approvals = get(admission.PREFIX+f'/actions/runs/{deployment["run_id"]}/approvals')
    require(type(approvals) is list and len(approvals) <= 100)
    matching = []
    for approval in approvals:
        require(type(approval) is dict and type(approval.get('environments')) is list
                and len(approval['environments']) <= 20)
        entries = [entry for entry in approval['environments'] if type(entry) is dict and entry.get('id') == env['id']]
        if entries:
            require(len(entries) == 1 and type(entries[0]['id']) is int
                    and entries[0].get('name') == stage and entries[0].get('url') == env['url'])
            matching.append(approval)
    approver = None
    if reviewers:
        require(len(matching) == 1 and matching[0].get('state') == 'approved')
        user = matching[0].get('user')
        approver = admission.user(user)
        require(approver in reviewers and approver not in initiators
                and user['login'].casefold() == reviewers[approver])
    else:
        require(not matching)
    snapshot = {'environment_id':env['id'], 'name':stage, 'reviewer_id':approver,
                'required_reviewer_ids':sorted(reviewers),
                'policy_sha256':records.canonical_sha256({'environment':env,'branches':policies})}
    return snapshot


def validate(stage, controller, deployment, *, read, read_graphql, parse_workflow):
    """Native proof is refreshed after quality checks and remains non-deployable."""
    try:
        controller, deployment = copy.deepcopy((controller,deployment))
        records.validate_controller(controller)
        require(type(stage) is str and stage in STAGES and controller['run_attempt'] == 1
                and type(deployment) is dict and set(deployment) == {'run_id','run_attempt','job_id'}
                and admission.positive(deployment['run_id']) and admission.positive(deployment['job_id'])
                and type(deployment['run_attempt']) is int and deployment['run_attempt'] == 1
                and deployment['run_id'] != controller['run_id'] and callable(read) and callable(parse_workflow))
        def get(path):
            value = copy.deepcopy(read(path))
            require(type(value) in (dict,list))
            return value
        sha = controller['commit']
        repo = get(admission.PREFIX)
        repository(repo)
        require(repo.get('private') is False and repo.get('fork') is False and repo.get('default_branch') == 'main')
        def protection():
            branch = get(admission.PREFIX+'/branches/main')
            require(branch.get('name') == 'main' and branch.get('protected') is True
                    and type(branch.get('commit')) is dict and branch['commit'].get('sha') == sha)
            policy = get(admission.PREFIX+'/branches/main/protection')
            admission.protected_main(policy,sha,read_graphql)
            return policy
        initial_policy = protection()
        run_path = admission.PREFIX+f'/actions/runs/{deployment["run_id"]}'
        initial_run = run(get(run_path),controller,deployment)
        def workflow_identity():
            value = get(admission.PREFIX+f'/actions/workflows/{initial_run["workflow_id"]}')
            require(type(value.get('id')) is int and value['id'] == initial_run['workflow_id']
                    and value.get('path') == WORKFLOW and value.get('state') == 'active')
        workflow_identity()
        blob = workflow(get(admission.PREFIX+'/contents/'+WORKFLOW+'?ref='+sha),stage,parse_workflow)
        job_path = admission.PREFIX+f'/actions/jobs/{deployment["job_id"]}'
        job(get(job_path),stage,controller,deployment)
        initiators = {initial_run['actor'],initial_run['triggering_actor']}
        initial_environment = environment(get,stage,deployment,initiators)
        checks = admission.verify_checks(get,sha)
        source_approval=admission.verify_source_approval(get,sha,initiators)
        require(run(get(run_path),controller,deployment) == initial_run)
        require(protection() == initial_policy)
        workflow_identity()
        job(get(job_path),stage,controller,deployment)
        require(environment(get,stage,deployment,initiators) == initial_environment)
        result = {'schema_version':1,'artifact_kind':'native-deployment-admission', 'stage':stage,
                  'controller':controller,'deployment':deployment,'repository_id':admission.REPOSITORY_ID,
                  'workflow_blob':blob, **initial_environment, 'checks':checks,'source_approval':source_approval,'admitted':True,'deployable':False}
        result['evidence_sha256'] = records.canonical_sha256(result)
        records.bounded_tree(result)
        return result
    except Exception:
        raise DeployAdmissionError(ERROR) from None
