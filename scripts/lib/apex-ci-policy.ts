// Infra-only configuration exception; this checker never authorizes publication.
// Canonical sibling CI mirrors and signer/PR policy remain unchanged.
import { checkWorkflow, trustedReleaseGuard } from './ci-policy';
import { readFileSync, readdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

declare const Bun: { YAML: { parse(text: string): unknown } };

type Mapping = Record<string, unknown>;
const mapping = (value: unknown): value is Mapping => value !== null && typeof value === 'object' && !Array.isArray(value);
const exactKeys = (value: unknown, keys: readonly string[]): value is Mapping => mapping(value)
  && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
const sameFields = (value: unknown, expected: Mapping): boolean => exactKeys(value, Object.keys(expected))
  && Object.entries(expected).every(([key, expectedValue]) => value[key] === expectedValue);

export const publisherRepository = 'Xynes-Studio/xynes-infra' as const;
export const publisherRegistry = 'ghcr.io/xynes-studio/apex' as const;
export const publisherEnvironment = 'release' as const;
export const publisherGuard = `github.repository == '${publisherRepository}' && ${trustedReleaseGuard}`;
export const publisherCommand = 'python3 scripts/release/publish_images.py' as const;
export const publisherPermissions = Object.freeze({ contents: 'read', actions: 'read', packages: 'write' } as const);
export const productSmokeCommand = 'sudo --preserve-env=GITHUB_ACTIONS,GITHUB_REPOSITORY,GITHUB_SHA,GITHUB_REF,GITHUB_REF_PROTECTED,GITHUB_EVENT_NAME,GITHUB_RUN_ID,GITHUB_RUN_ATTEMPT,GITHUB_JOB,GITHUB_ACTOR,GITHUB_API_URL,GITHUB_SERVER_URL,GITHUB_WORKFLOW_REF,APEX_REGISTRY_PREFIX,GH_TOKEN,RUNNER_TEMP,APEX_ADMISSION_RECEIPT,APEX_BUILD_RECEIPT,APEX_MIGRATION_RECEIPT,APEX_CI_RUNNER_ENVIRONMENT,APEX_CI_RUNNER_OS /usr/bin/python3 scripts/release/ci_smoke.py';
const smokeInputs = Object.freeze({GH_TOKEN:'${{ github.token }}',APEX_REGISTRY_PREFIX:publisherRegistry,
  APEX_ADMISSION_RECEIPT:'${{ needs.admission.outputs.receipt }}',APEX_BUILD_RECEIPT:'${{ needs.build-images.outputs.receipt }}',
  APEX_MIGRATION_RECEIPT:'${{ needs.build-images.outputs.migration_receipt }}',
  APEX_CI_RUNNER_ENVIRONMENT:'${{ runner.environment }}',APEX_CI_RUNNER_OS:'${{ runner.os }}'});
export const controllerSparsePaths = '/scripts/release/\n/scripts/lib/\n/infra/release/' as const;
const publisherInputs = Object.freeze({
  GH_TOKEN: '${{ github.token }}',
  GH_POLICY_READ_TOKEN: '${{ secrets.RELEASE_POLICY_READ_TOKEN }}',
  APEX_REGISTRY_PREFIX: publisherRegistry,
  APEX_ADMISSION_RECEIPT: '${{ needs.admission.outputs.receipt }}',
  APEX_BUILD_RECEIPT: '${{ needs.build-images.outputs.receipt }}',
});
const publicationOutputs=Object.freeze({subjects:'${{ steps.publish.outputs.subjects }}',
  receipt:'{"id":${{ steps.publication.outputs.artifact-id }},"digest":"sha256:${{ steps.publication.outputs.artifact-digest }}"}'});
const publicationUpload=Object.freeze({name:'apex-publication-${{ github.run_id }}-${{ github.run_attempt }}',
  path:'${{ runner.temp }}/apex-publications/published-receipt.json','if-no-files-found':'error',
  'retention-days':14,'compression-level':0});

export type PackagePublisherRole = Readonly<{ kind: 'package-publisher' }>;
export type ApexPolicyContext = Readonly<{
  repository: typeof publisherRepository;
  registryPrefix: typeof publisherRegistry;
  roles: Readonly<{ 'publish-images'?: PackagePublisherRole }>;
}>;
type PublisherCheckout=Readonly<{uses:`actions/checkout@${string}`;with:Readonly<{
  ref:'${{ github.sha }}';'persist-credentials':false;
  'sparse-checkout':typeof controllerSparsePaths;'sparse-checkout-cone-mode':false;
}>}>;
type PublisherInvocation=Readonly<{run:typeof publisherCommand;env:typeof publisherInputs}>;
type PublicationUpload=Readonly<{id:'publication';uses:'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02';with:typeof publicationUpload}>;
export type PackagePublisherJob = Readonly<{
  name?: string;
  'runs-on': string;
  'timeout-minutes': number;
  needs: readonly ['admission', 'build-images', 'product-smoke'];
  if: string;
  environment: typeof publisherEnvironment;
  permissions: typeof publisherPermissions;
}> & (Readonly<{steps:readonly[PublisherCheckout,PublisherInvocation];outputs?:never}>
  | Readonly<{steps:readonly[PublisherCheckout,PublisherInvocation & {readonly id:'publish'},PublicationUpload];outputs:typeof publicationOutputs}>);

function guard(value: unknown): string {
  return typeof value === 'string' ? value.replace(/^\$\{\{\s*|\s*\}\}$/g, '').trim() : '';
}

function publisherErrors(job: unknown, pins: ReadonlySet<string>): string[] {
  const errors: string[] = [];
  const reject = (message: string) => errors.push(`publish-images: ${message}`);
  if (!mapping(job)) return ['publish-images: job must be a mapping'];
  const allowed = ['name', 'runs-on', 'timeout-minutes', 'needs', 'if', 'environment', 'permissions', 'steps','outputs'];
  if (Object.keys(job).some(key => !allowed.includes(key))) reject('publisher has unreviewed execution or inheritance fields');
  if (job.name !== undefined && (typeof job.name !== 'string' || job.name.includes('${{'))) reject('publisher name must be literal');
  if (guard(job.if) !== publisherGuard) reject('publisher requires exact infra manual protected-main guard');
  if (job.environment !== publisherEnvironment) reject('publisher requires the literal release environment');
  if (!Array.isArray(job.needs) || job.needs.length !== 3 || job.needs[0] !== 'admission' || job.needs[1] !== 'build-images' || job.needs[2] !== 'product-smoke')
    reject('publisher requires admission, build-images and product-smoke dependencies');
  if (!sameFields(job.permissions, publisherPermissions)) reject('publisher permissions must match its isolated role');
  const receiptHandoff=job.outputs!==undefined;
  if(receiptHandoff && !sameFields(job.outputs,publicationOutputs))reject('publisher output bindings must come from its checked publication steps');
  if (!Array.isArray(job.steps) || job.steps.length !== (receiptHandoff?3:2)) return [...errors, 'publish-images: only checked controller and publication receipt steps are allowed'];
  const [checkout, invoke] = job.steps;
  if (!exactKeys(checkout, ['uses', 'with']) || typeof checkout.uses !== 'string'
    || !/^actions\/checkout@[a-f0-9]{40}$/.test(checkout.uses) || !pins.has(checkout.uses)
    || !sameFields(checkout.with, { ref: '${{ github.sha }}', 'persist-credentials': false,
      'sparse-checkout': controllerSparsePaths, 'sparse-checkout-cone-mode': false }))
    reject('publisher checkout must be approved and read only exact current controller tools without persisted credentials');
  if (!exactKeys(invoke, receiptHandoff?['id','run','env']:['run', 'env'])
    || receiptHandoff && invoke.id!=='publish' || invoke.run !== publisherCommand || !sameFields(invoke.env, publisherInputs))
    reject('publisher must use only the fixed controller entrypoint and bound auth/receipt inputs');
  if(receiptHandoff){
    const upload=job.steps[2];
    if(!exactKeys(upload,['id','uses','with']) || upload.id!=='publication'
      || upload.uses!=='actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02'
      || !pins.has(String(upload.uses)) || !sameFields(upload.with,publicationUpload))
      reject('publisher may upload only its exact verified data receipt with the pinned action');
  }
  return errors;
}

function smokeErrors(job: unknown, pins: ReadonlySet<string>): string[] {
  const failure = ['product-smoke: requires exact read-only preapproval image-smoke job'];
  if (!exactKeys(job, ['name','if','needs','runs-on','timeout-minutes','permissions','steps'])
    || job.name !== 'product-smoke' || guard(job.if) !== publisherGuard || job['runs-on'] !== 'ubuntu-24.04'
    || job['timeout-minutes'] !== 30 || !Array.isArray(job.needs)
    || job.needs.length !== 2 || job.needs[0] !== 'admission' || job.needs[1] !== 'build-images'
    || !sameFields(job.permissions,{contents:'read',actions:'read'}) || !Array.isArray(job.steps) || job.steps.length !== 3)
    return failure;
  const [checkout,invoke,upload] = job.steps;
  if (!exactKeys(checkout,['uses','with']) || checkout.uses !== 'actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683'
    || !pins.has(String(checkout.uses)) || !sameFields(checkout.with,{ref:'${{ github.sha }}','persist-credentials':false,'fetch-depth':0})
    || !exactKeys(invoke,['run','env']) || invoke.run !== productSmokeCommand || !sameFields(invoke.env,smokeInputs)
    || !exactKeys(upload,['id','if','uses','with']) || upload.id !== 'smoke-result' || upload.if !== 'always()'
    || upload.uses !== 'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02' || !pins.has(String(upload.uses))
    || !sameFields(upload.with,{name:'apex-smoke-${{ github.run_id }}-${{ github.run_attempt }}',
      path:'${{ runner.temp }}/apex-ci-smoke/result.json','if-no-files-found':'error','retention-days':14,'compression-level':0})) return failure;
  return [];
}

/** Return configuration errors only. Empty errors never prove independent approval,
 * private environment entitlements, authenticated receipts or publication eligibility.
 * The future fixed entrypoint must reverify those proofs and refuse if any is missing.
 * Receipt inputs above are opaque references, never booleans or proof by themselves.
 */
export function checkApexWorkflow(value: unknown, pins: ReadonlySet<string>, context: unknown): string[] {
  if (!exactKeys(context, ['repository', 'registryPrefix', 'roles'])
    || context.repository !== publisherRepository || context.registryPrefix !== publisherRegistry
    || !mapping(context.roles) || Object.keys(context.roles).some(key => key !== 'publish-images')
    || Object.values(context.roles).some(role => !sameFields(role, { kind: 'package-publisher' })))
    return ['Apex role context must be closed, infra-bound and registry-bound'];
  if (!Object.hasOwn(context.roles, 'publish-images')) return checkWorkflow(value, pins);
  if (!mapping(value) || !mapping(value.jobs)) return ['Apex publisher workflow must have jobs'];
  const errors: string[] = [];
  const events = value.on ?? value.true;
  if ((Object.hasOwn(value, 'on') && Object.hasOwn(value, 'true'))
    || !exactKeys(events, ['workflow_dispatch']) || !exactKeys(events.workflow_dispatch, []))
    errors.push('Apex publisher workflow must allow only manual dispatch without untrusted inputs');
  if (Object.hasOwn(value, 'env') || Object.hasOwn(value, 'defaults'))
    errors.push('Apex publisher workflow must not inherit environment or shell overrides');
  for (const id of ['admission', 'build-images']) {
    const job = value.jobs[id];
    if (!Object.hasOwn(value.jobs, id) || !mapping(job)
      || (job.permissions !== undefined && (!mapping(job.permissions) || Object.values(job.permissions).some(level => level === 'write'))))
      errors.push('Apex publisher requires read-only admission and build-images jobs');
  }
  errors.push(...smokeErrors(value.jobs['product-smoke'],pins));
  const job = value.jobs['publish-images'];
  const publisherFailures = publisherErrors(job, pins);
  errors.push(...publisherFailures);
  // Normalize only this completely checked publisher, in a fresh mapping used
  // solely for canonical validation. Never emit a normalized workflow or mutate
  // permissions used by Actions. Every other job keeps its original privileges.
  const normalized = publisherFailures.length === 0 && mapping(job)
    ? { ...value, jobs: { ...value.jobs, 'publish-images': {
      ...job, permissions: { contents: 'read', actions: 'read', packages: 'read' },
    } } } : value;
  return [...errors, ...checkWorkflow(normalized, pins)];
}

/** The only role exception is the named infra build workflow. Other workflows
 * use the unchanged shared policy, including its signer and PR restrictions. */
export function inspectApexRepository(root: string): string[] {
  const pinsValue: unknown = JSON.parse(readFileSync(resolve(root, '.github/ci/action-pins.json'), 'utf8'));
  if (!Array.isArray(pinsValue) || !pinsValue.every(pin => typeof pin === 'string'))
    return ['action pins must be an array of strings'];
  const pins = new Set<string>(pinsValue);
  const directory = resolve(root, '.github/workflows');
  const files = readdirSync(directory).filter(file => /\.ya?ml$/.test(file));
  if (!files.length) return ['no workflows found'];
  const names = new Set<string>();
  const failures = files.flatMap(file => {
    const value = Bun.YAML.parse(readFileSync(resolve(directory, file), 'utf8'));
    if (mapping(value) && mapping(value.jobs)) {
      for (const [id, job] of Object.entries(value.jobs)) {
        if (!mapping(job)) continue;
        if (job.name === '${{ matrix.gate }}' && mapping(job.strategy)
          && mapping(job.strategy.matrix) && Array.isArray(job.strategy.matrix.gate)) {
          for (const name of job.strategy.matrix.gate) if (typeof name === 'string') names.add(name);
        } else if (job.name === undefined || typeof job.name === 'string' && !job.name.includes('${{'))
          names.add(typeof job.name === 'string' ? job.name : id);
      }
    }
    const errors = file === 'apex-build.yml' ? checkApexWorkflow(value, pins, {
      repository: publisherRepository, registryPrefix: publisherRegistry,
      roles: { 'publish-images': { kind: 'package-publisher' } },
    }) : checkWorkflow(value, pins);
    return errors.map(error => `${file}: ${error}`);
  });
  const required: unknown = JSON.parse(readFileSync(resolve(root, '.github/ci/required-checks.json'), 'utf8'));
  if (!Array.isArray(required) || !required.length || !required.every(name => typeof name === 'string' && name.length))
    return [...failures, 'required check names must be a nonempty array of strings'];
  for (const name of required) if (!names.has(name)) failures.push(`Missing required check producer: ${name}`);
  return failures;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    const failures = inspectApexRepository(resolve(process.argv[2] ?? '.'));
    if (failures.length) { console.error(failures.join('\n'));process.exitCode = 1; }
    else console.log('PASS: immutable actions, isolated jobs and infra publisher role policy');
  } catch { console.error('CI policy input is invalid or unavailable');process.exitCode = 1; }
}
