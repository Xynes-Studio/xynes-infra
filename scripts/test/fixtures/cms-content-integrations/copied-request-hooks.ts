/** Runs inside an owned CMS .tmp directory; imports production APIs without editing them. */
import { afterAll, afterEach, expect } from "bun:test";
import assert from "node:assert/strict";
import { generateKeyPairSync } from "node:crypto";
import { writeFileSync } from "node:fs";
import { writeFile } from "node:fs/promises";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { eq } from "drizzle-orm";
import { z } from "zod";
import { db } from "../../src/infra/db";
import {
	createEntry,
	publishEntryByIdAndWorkspace,
	setEntryStatusByIdAndWorkspace,
	updateEntryByIdAndWorkspaceScoped,
} from "../../src/infra/db/repositories/content-entry.repository";
import {
	contentDirectories,
	contentEntries,
	contentTypes,
} from "../../src/infra/db/schema";
import {
	type Builder,
	type CopiedRequest,
	credentialHeaders,
	prepareCopiedRequest,
} from "./copied-request";

assert.equal(
	process.env.RUN_CMS_CONTENT_INTEGRATIONS_TESTS,
	"true",
	"Explicit A5 fixture required",
);
const source = process.env.XYNES_FRONTEND_REPO;
const evidence = z.string().min(1).parse(process.env.XYNES_A5_EVIDENCE_DIR);
assert.ok(source && evidence, "Missing owned A5 inputs");
const module: unknown = await import(
	pathToFileURL(join(source, "src/features/content-integrations/index.ts")).href
);
const builder = z
	.object({
		buildIntegrationRequest: z.custom<Builder>(
			(value) => typeof value === "function",
		),
	})
	.parse(module).buildIntegrationRequest;
const originalFetch = globalThis.fetch;
const captures: {
	request: CopiedRequest;
	status: number;
	response: unknown;
}[] = [];
const rawStatuses: number[] = [];
const internalPaths = new Set<string>();
let fixture:
	| { origin: string; workspaceId: string; folder: string; key: string }
	| undefined;
let extended = false;
let snapshotsRetained = false;
let snapshotBaseline: string | undefined;
const checks: string[] = [];
const intercept = async (
	input: Parameters<typeof fetch>[0],
	init?: Parameters<typeof fetch>[1],
) => {
	const url = new URL(input instanceof Request ? input.url : String(input));
	assert.ok(
		["127.0.0.1", "localhost"].includes(url.hostname),
		"Fixture attempted outbound network",
	);
	const key = new Headers(init?.headers).get("X-XS-API-Key");
	if (!key) internalPaths.add(url.pathname);
	if (
		!key ||
		(init?.method && init.method !== "GET") ||
		!url.pathname.includes("/delivery/entries")
	)
		return originalFetch(input, init);
	const copied = prepareCopiedRequest(builder, url);
	const response = copied
		? await originalFetch(copied.url, {
				...init,
				headers: credentialHeaders(copied, key),
			})
		: await originalFetch(input, init);
	if (copied) {
		captures.push({
			request: copied,
			status: response.status,
			response: await response.clone().json(),
		});
		if (!fixture && response.status === 200 && copied.kind === "directory") {
			const workspaceId = url.pathname.split("/")[2];
			const folder = url.searchParams.get("directoryId");
			assert.ok(folder, "Missing fixture folder");
			fixture = { origin: url.origin, workspaceId, folder, key };
		}
	} else rawStatuses.push(response.status);
	return response;
};

async function send(path: string) {
	assert.ok(fixture, "Missing live copied-request context");
	return fetch(new URL(path, fixture.origin), {
		headers: { "X-XS-API-Key": fixture.key },
	});
}
const feed = z
	.object({
		ok: z.literal(true),
		data: z
			.object({
				items: z.array(
					z.object({ id: z.string(), title: z.string().optional() }).strict(),
				),
				page: z
					.object({
						limit: z.number(),
						offset: z.number(),
						hasMore: z.boolean(),
					})
					.strict(),
			})
			.strict(),
		meta: z.unknown().optional(),
	})
	.strict();
async function ids(path: string) {
	const response = await send(path);
	expect(response.status).toBe(200);
	return feed.parse(await response.json()).data.items.map((item) => item.id);
}
export function installCopiedRequestHooks() {
	// Current platform calls use the real bound-request signer/receiver. Only this
	// process owns these keys; the launcher removes its entire .tmp directory.
	const identity = generateKeyPairSync("ed25519");
	const privateFile = join(import.meta.dir, "gateway-private.pem");
	const trustFile = join(import.meta.dir, "gateway-trust.json");
	const keyId = "a5-fixture-gateway";
	writeFileSync(
		privateFile,
		identity.privateKey.export({ type: "pkcs8", format: "pem" }),
		{ mode: 0o600 },
	);
	writeFileSync(
		trustFile,
		JSON.stringify([
			{
				issuer: "gateway",
				keyId,
				publicKey: identity.publicKey.export({ type: "spki", format: "pem" }),
			},
		]),
		{ mode: 0o600 },
	);
	process.env.INTERNAL_REQUEST_PRIVATE_KEY_FILE = privateFile;
	process.env.INTERNAL_REQUEST_KEY_ID = keyId;
	process.env.INTERNAL_REQUEST_TRUST_FILE = trustFile;

	globalThis.fetch = Object.assign(intercept, {
		preconnect: originalFetch.preconnect,
	});
	afterEach(async () => {
		if (!fixture) return;
		if (extended) {
			const rows = await db
				.select({
					id: contentEntries.id,
					snapshot: contentEntries.publishedSnapshot,
				})
				.from(contentEntries)
				.where(eq(contentEntries.workspaceId, fixture.workspaceId))
				.orderBy(contentEntries.id);
			const current = JSON.stringify(rows);
			if (captures.slice(-2).every((row) => row.status === 404)) {
				assert.ok(snapshotBaseline, "Missing pre-rollback snapshot evidence");
				snapshotsRetained =
					current === snapshotBaseline &&
					rows.some((row) => row.snapshot !== null);
			} else snapshotBaseline = current;
			return;
		}
		extended = true;
		const { workspaceId, folder } = fixture;
		const path = `/workspaces/${workspaceId}/delivery/entries`;
		const child = crypto.randomUUID(),
			sibling = crypto.randomUUID(),
			foreignFolder = crypto.randomUUID();
		const foreignWorkspace = crypto.randomUUID();
		await db.insert(contentDirectories).values([
			{
				id: child,
				workspaceId,
				parentId: folder,
				name: "Child",
				pathSegment: "child",
			},
			{ id: sibling, workspaceId, name: "Sibling", pathSegment: "sibling" },
			{
				id: foreignFolder,
				workspaceId: foreignWorkspace,
				name: "Foreign",
				pathSegment: "foreign",
			},
		]);
		const [type] = await db
			.select()
			.from(contentTypes)
			.where(eq(contentTypes.workspaceId, workspaceId));
		assert.ok(type, "Missing fixture content type");
		const published = await createEntry({
			workspaceId,
			contentTypeId: type.id,
			directoryId: child,
			status: "published",
			data: {
				slug: "a5-child",
				title: "Child publication",
				description: "Fixture",
				tags: [],
			},
		});
		const draft = await createEntry({
			workspaceId,
			contentTypeId: type.id,
			directoryId: child,
			status: "draft",
			data: {
				slug: "a5-draft",
				title: "Draft",
				description: "Fixture",
				tags: [],
			},
		});
		expect(await ids(`${path}?directoryId=${child}&fields=id,title`)).toEqual([
			published.id,
		]);
		expect(
			(await ids(`${path}?directoryId=${folder}&fields=id,title`)).includes(
				published.id,
			),
		).toBe(false);
		expect(
			await ids(`${path}?directoryId=${foreignFolder}&fields=id,title`),
		).toEqual([]);
		expect((await send(`${path}/${draft.id}`)).status).toBe(404);
		const foreignEntry = await createEntry({
			workspaceId: foreignWorkspace,
			contentTypeId: type.id,
			directoryId: foreignFolder,
			status: "published",
			data: {
				slug: "a5-foreign",
				title: "Foreign",
				description: "Fixture",
				tags: [],
			},
		});
		expect((await send(`${path}/${foreignEntry.id}`)).status).toBe(404);
		checks.push(
			"direct-folder-only",
			"foreign-folder-empty",
			"draft-and-foreign-entry-hidden",
		);
		await updateEntryByIdAndWorkspaceScoped({
			entryId: published.id,
			workspaceId,
			directoryId: sibling,
			data: {
				slug: "a5-child",
				title: "Moved draft",
				description: "Fixture",
				tags: [],
			},
		});
		expect(await ids(`${path}?directoryId=${child}&fields=id,title`)).toEqual([
			published.id,
		]);
		expect(await ids(`${path}?directoryId=${sibling}&fields=id,title`)).toEqual(
			[],
		);
		expect(
			await (await send(`${path}/${published.id}?fields=title`)).json(),
		).toMatchObject({ data: { entry: { title: "Child publication" } } });
		await publishEntryByIdAndWorkspace({ entryId: published.id, workspaceId });
		expect(await ids(`${path}?directoryId=${child}&fields=id,title`)).toEqual(
			[],
		);
		expect(await ids(`${path}?directoryId=${sibling}&fields=id,title`)).toEqual(
			[published.id],
		);
		expect(
			await (await send(`${path}/${published.id}?fields=title`)).json(),
		).toMatchObject({ data: { entry: { title: "Moved draft" } } });
		checks.push("save-keeps-published-folder", "republish-moves-membership");
		// A harmless object reference proves JSON delivery does not need storage signing/fetching.
		const asset = await createEntry({
			workspaceId,
			contentTypeId: type.id,
			directoryId: sibling,
			status: "published",
			data: {
				slug: "a5-asset",
				title: "Asset reference",
				description: "Fixture",
				tags: [],
				body: {
					root: {
						type: "root",
						version: 1,
						children: [
							{
								type: "image-block",
								version: 1,
								objectId: crypto.randomUUID(),
								src: "",
							},
						],
					},
				},
			},
		});
		const assetResponse = await send(`${path}/${asset.id}?fields=body`);
		expect(assetResponse.status).toBe(200);
		expect(await assetResponse.json()).toMatchObject({
			data: {
				entry: {
					id: asset.id,
					body: {
						root: {
							type: "root",
							children: [{ type: "image-block", src: "" }],
						},
					},
				},
			},
		});
		checks.push("body-object-reference-no-signed-url");
		// Real loopback reverse proxy configured with a path prefix; send B's exact URL unchanged.
		const origin = fixture.origin;
		const prefix = "/api-prefix";
		const proxy = Bun.serve({
			hostname: "127.0.0.1",
			port: 0,
			fetch: async (request) => {
				const target = new URL(request.url);
				assert.ok(
					target.pathname.startsWith(`${prefix}/`),
					"Invalid fixture prefix",
				);
				target.pathname = target.pathname.slice(prefix.length);
				return originalFetch(origin + target.pathname + target.search, {
					headers: request.headers,
				});
			},
		});
		try {
			const produced = z
				.object({
					ok: z.literal(true),
					request: z
						.object({
							kind: z.literal("directory"),
							method: z.literal("GET"),
							url: z.string(),
							headers: z
								.object({
									Authorization: z.literal(`Bearer \${XYNES_API_KEY}`),
								})
								.strict(),
							fields: z.array(z.string()),
						})
						.passthrough(),
				})
				.parse(
					builder(
						{
							workspaceId,
							workspaceSlug: "a5-fixture",
							apiBaseUrl: proxy.url.origin + prefix,
							target: {
								kind: "directory",
								directoryId: sibling,
								label: "Sibling",
								breadcrumb: "Fixture / Sibling",
							},
						},
						{
							fields: ["title"],
							sortBy: "title",
							sortDirection: "asc",
							limit: 100,
							offset: 0,
						},
					),
				);
			const copied = produced.request;
			assert.ok(
				copied.url.startsWith(`${proxy.url.origin + prefix + path}?`),
				"Invalid prefixed producer URL",
			);
			const response = await originalFetch(copied.url, {
				headers: credentialHeaders(copied, fixture.key),
			});
			expect(response.status).toBe(200);
			const payload = await response.json();
			expect(
				feed
					.parse(payload)
					.data.items.map((item) => item.id)
					.sort(),
			).toEqual([published.id, asset.id].sort());
			captures.push({
				request: copied,
				status: response.status,
				response: payload,
			});
			checks.push("configured-base-path-prefix");
		} finally {
			proxy.stop(true);
		}
		for (const entry of [published, asset])
			await setEntryStatusByIdAndWorkspace({
				entryId: entry.id,
				workspaceId,
				status: "archived",
			});
	}, 30000);

	afterAll(async () => {
		globalThis.fetch = originalFetch;
		fixture = undefined;
		expect(extended).toBe(true);
		expect(checks).toHaveLength(7);
		for (const status of [200, 401, 403, 404])
			expect(captures.some((row) => row.status === status)).toBe(true);
		expect(
			rawStatuses.filter((status) => status === 400).length,
		).toBeGreaterThanOrEqual(14);
		expect(rawStatuses.every((status) => status === 400)).toBe(true);
		expect(snapshotsRetained).toBe(true);
		expect([...internalPaths]).toEqual(["/internal/cms-actions"]);
		checks.push(
			"disabled-route-rollback-retains-snapshots",
			"only-cms-internal-fetches",
		);
		const serialized = JSON.stringify(
			{
				checks,
				revisions: Object.fromEntries(
					[
						"CONTRACTS",
						"CMS",
						"FRONTEND",
						"PLATFORM_CONFIG",
						"INFRA",
						"GATEWAY",
						"ACCOUNTS",
					].map((name) => [name, process.env[`XYNES_${name}_REVISION`]]),
				),
				rawNegativeStatuses: rawStatuses,
				copiedRequests: captures,
			},
			null,
			2,
		);
		assert.ok(
			!/xynes_live_[0-9a-f]{64}/.test(serialized),
			"Unsafe credential in evidence",
		);
		await writeFile(join(evidence, "copied-requests.json"), serialized, {
			mode: 0o600,
		});
	});
}
