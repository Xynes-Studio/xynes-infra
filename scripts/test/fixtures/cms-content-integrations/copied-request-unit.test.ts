import { describe, expect, it } from "bun:test";
import { credentialHeaders, prepareCopiedRequest } from "./copied-request";

const workspace = "11111111-1111-4111-8111-111111111111";
const folder = "22222222-2222-4222-8222-222222222222";
const uri = new URL(
	`http://127.0.0.1:4100/workspaces/${workspace}/delivery/entries?directoryId=${folder}&limit=2&search=true&fields=title`,
);
const generated = {
	ok: true,
	request: {
		kind: "directory",
		method: "GET",
		url: `http://127.0.0.1:4100/workspaces/${workspace}/delivery/entries?directoryId=${folder}&limit=2&search=true&fields=id%2Ctitle`,
		headers: { Authorization: `Bearer \${XYNES_API_KEY}` },
		fields: ["id", "title"],
	},
};
describe("copied request adapter boundary (test-only builder seam)", () => {
	it("passes fixture context/options to the producer and keeps its exact URL/header", () => {
		let input: unknown, options: unknown;
		const copied = prepareCopiedRequest((context, selected) => {
			input = context;
			options = selected;
			return generated;
		}, uri);
		expect(input).toMatchObject({
			workspaceId: workspace,
			apiBaseUrl: uri.origin,
			target: { kind: "directory", directoryId: folder },
		});
		expect(options).toEqual({ limit: 2, search: "true", fields: ["title"] });
		expect(copied?.url).toBe(generated.request.url);
		expect(copied?.headers).toEqual(generated.request.headers);
		if (!copied) throw new Error("Expected copied request");
		const key = `xynes_live_${"a".repeat(64)}`;
		expect(
			credentialHeaders(copied, key).get("Authorization") === `Bearer ${key}`,
		).toBe(true);
		expect(copied.headers.Authorization).toBe(`Bearer \${XYNES_API_KEY}`);
	});
	it("leaves raw invalid transport cases untouched rather than broadening them", () => {
		for (const suffix of [
			"&limit=2",
			"&workspaceId=override",
			"&offset=0x10",
			"&preview=true",
		]) {
			let calls = 0;
			expect(
				prepareCopiedRequest(
					() => {
						calls++;
						return generated;
					},
					new URL(uri.href + suffix),
				),
			).toBeNull();
			expect(calls).toBe(0);
		}
		expect(
			prepareCopiedRequest(
				() => ({ ok: false, error: { code: "INVALID_OPTIONS" } }),
				uri,
			),
		).toBeNull();
	});
	it("rejects off-fixture producer output or real-key-shaped producer headers", () => {
		for (const request of [
			{ ...generated.request, url: "https://outside.example.com/" },
			{ ...generated.request, url: `${uri.origin}/wrong-path` },
			{ ...generated.request, method: "POST" },
			{
				...generated.request,
				headers: { Authorization: "Bearer must-not-echo" },
			},
			{
				...generated.request,
				headers: { ...generated.request.headers, Other: "value" },
			},
			{ ...generated.request, fields: ["body"] },
		])
			expect(() =>
				prepareCopiedRequest(() => ({ ok: true, request }), uri),
			).toThrow();
		const copied = prepareCopiedRequest(() => generated, uri);
		if (!copied) throw new Error("Expected copied request");
		expect(() => credentialHeaders(copied, "bad\ncredential")).toThrow(
			"Invalid fixture credential",
		);
	});
});
