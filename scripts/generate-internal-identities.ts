/** Generate a fresh owner-isolated key set; refuse existing directories. */
import { generateKeyPairSync } from 'node:crypto';
import { mkdirSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';

const directory = process.argv[2];
if (!directory) throw new Error('Usage: bun --no-env-file scripts/generate-internal-identities.ts <new-directory>');
const destination = resolve(directory);
mkdirSync(destination, { mode: 0o700 });
const owners = ['gateway', 'accounts', 'cms', 'docs', 'storage', 'telemetry'] as const;
const ids = { gateway: 'g1', accounts: 'a1', cms: 'c1', docs: 'd1', storage: 's1', telemetry: 't1' };
const publicEntries = owners.map((issuer) => {
  const identity = generateKeyPairSync('ed25519');
  writeFileSync(join(destination, `${issuer}-private.pem`), identity.privateKey.export({ type: 'pkcs8', format: 'pem' }), { mode: 0o600, flag: 'wx' });
  return { issuer, keyId: ids[issuer], publicKey: identity.publicKey.export({ type: 'spki', format: 'pem' }) };
});
for (const receiver of ['accounts', 'authz', 'cms', 'docs', 'storage', 'telemetry']) {
  const issuers = receiver === 'authz' ? ['gateway', 'accounts', 'cms', 'docs'] : ['gateway'];
  writeFileSync(join(destination, `${receiver}-trust.json`), JSON.stringify(publicEntries.filter((entry) => issuers.includes(entry.issuer)), null, 2) + '\n', { mode: 0o600, flag: 'wx' });
}
console.log('Generated owner-only private identities and receiver public trust files.');
