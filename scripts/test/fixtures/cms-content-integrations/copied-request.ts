/** Test-only handoff: preserve the real producer's request; never broaden invalid input. */
import assert from "node:assert/strict";

export type Builder = (context: unknown, options: unknown) => unknown;
export interface CopiedRequest {
	kind: "directory" | "entry";
	method: "GET";
	url: string;
	headers: { Authorization: string };
	fields: string[];
}
function record(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}
const uuid = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
export function prepareCopiedRequest(
	builder: Builder,
	original: URL,
): CopiedRequest | null {
	const match = original.pathname.match(
		new RegExp(`^/workspaces/(${uuid})/delivery/entries(?:/(${uuid}))?$`, "i"),
	);
	if (!match) return null;
	const [, workspaceId, entryId] = match;
	const kind = entryId ? "entry" : "directory";
	const allowed = entryId
		? ["fields"]
		: [
				"directoryId",
				"sortBy",
				"sortDirection",
				"limit",
				"offset",
				"search",
				"fields",
			];
	const seen = new Set<string>();
	const options: Record<string, unknown> = {};
	for (const [key, value] of original.searchParams) {
		if (seen.has(key) || !allowed.includes(key)) return null;
		seen.add(key);
		if (key === "directoryId") continue;
		if (key === "limit" || key === "offset") {
			if (!/^\d+$/.test(value)) return null;
			options[key] = Number(value);
		} else
			options[key] =
				key === "fields" ? value.split(",").map((item) => item.trim()) : value;
	}
	const directoryId = original.searchParams.get("directoryId");
	if (!entryId && !directoryId) return null;
	const result = builder(
		{
			workspaceId,
			workspaceSlug: "a5-fixture",
			apiBaseUrl: original.origin,
			target: entryId
				? { kind, entryId, label: "Fixture entry" }
				: {
						kind,
						directoryId,
						label: "Fixture folder",
						breadcrumb: "Fixture / Delivery",
					},
		},
		options,
	);
	if (!record(result) || result.ok !== true) return null;
	const request = result.request;
	assert.ok(record(request), "Invalid producer request");
	assert.ok(
		request.kind === kind && request.method === "GET",
		"Invalid producer operation",
	);
	assert.ok(typeof request.url === "string", "Invalid producer URL");
	const url = new URL(request.url);
	assert.ok(
		url.origin === original.origin &&
			url.pathname === original.pathname &&
			!url.username &&
			!url.password &&
			!url.hash,
		"Producer escaped fixture boundary",
	);
	assert.ok(
		record(request.headers) &&
			Object.keys(request.headers).length === 1 &&
			request.headers.Authorization === `Bearer \${XYNES_API_KEY}`,
		"Invalid producer credential placeholder",
	);
	const fields = request.fields;
	const summaryFields = ["id", "title", "description", "tags", "publishedAt"];
	assert.ok(
		Array.isArray(fields) &&
			fields.length > 0 &&
			fields[0] === "id" &&
			fields.every(
				(field: unknown): field is string =>
					typeof field === "string" &&
					(summaryFields.includes(field) ||
						(kind === "entry" && field === "body")),
			),
		"Invalid producer fields",
	);
	return {
		kind,
		method: "GET",
		url: request.url,
		headers: { Authorization: request.headers.Authorization },
		fields,
	};
}
export function credentialHeaders(
	request: CopiedRequest,
	key: string,
): Headers {
	assert.ok(
		/^xynes_live_[0-9a-f]{64}$/.test(key),
		"Invalid fixture credential",
	);
	return new Headers({
		Authorization: request.headers.Authorization.replace(
			`\${XYNES_API_KEY}`,
			key,
		),
	});
}
