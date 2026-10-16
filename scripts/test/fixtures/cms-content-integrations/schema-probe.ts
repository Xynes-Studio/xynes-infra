import { deepEqual, ok } from "node:assert/strict";
import { pathToFileURL } from "node:url";

const source = process.env.XYNES_A5_SCHEMA_SOURCE;
ok(source, "Missing schema probe source");
const moduleExports: unknown = await import(pathToFileURL(source).href);
ok(moduleExports !== null && typeof moduleExports === "object");
function exported(name: string): unknown {
	ok(moduleExports !== null && typeof moduleExports === "object");
	const value: unknown = Reflect.get(moduleExports, name);
	return value;
}
function parse(schema: unknown, payload: unknown) {
	ok(schema !== null && typeof schema === "object");
	const safeParse: unknown = Reflect.get(schema, "safeParse");
	ok(typeof safeParse === "function");
	const result: unknown = Reflect.apply(safeParse, schema, [payload]);
	ok(result !== null && typeof result === "object" && "success" in result);
	ok(typeof result.success === "boolean");
	const data: unknown = Reflect.get(result, "data");
	return { success: result.success, data };
}
const cases: unknown = JSON.parse(process.env.XYNES_A5_SCHEMA_CASES ?? "null");
ok(Array.isArray(cases) && cases.length > 0, "Missing schema probe cases");
for (const candidate of cases) {
	const test: unknown = candidate;
	ok(test !== null && typeof test === "object");
	ok(
		"target" in test &&
			(test.target === "directory" || test.target === "entry"),
	);
	ok(
		"payload" in test && "success" in test && typeof test.success === "boolean",
	);
	const schema = exported(
		test.target === "directory"
			? "ContentDeliveryDirectoryPayloadSchema"
			: "ContentDeliveryEntryPayloadSchema",
	);
	const actual = parse(schema, test.payload);
	deepEqual(
		actual.success,
		test.success,
		"Schema acceptance differs from frozen contract",
	);
	if ("data" in test)
		deepEqual(
			actual.data,
			test.data,
			"Schema defaults/projection differ from frozen contract",
		);
}
console.log(
	JSON.stringify({
		checksPassed: cases.length,
		directoryFields: exported("DELIVERY_SUMMARY_FIELDS"),
		entryFields: exported("DELIVERY_DETAIL_FIELDS"),
	}),
);
