import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { describe, expect, it } from "vitest";
import { compareLocaleKeys } from "./check-locale-parity.mjs";

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const checkerPath = resolve(scriptDirectory, "check-locale-parity.mjs");
const fixturePath = (name, locale) =>
  resolve(scriptDirectory, "fixtures", "locale-parity", name, `${locale}.json`);

const readFixture = async (name, locale) =>
  JSON.parse(await readFile(fixturePath(name, locale), "utf8"));

const readPackage = async () =>
  JSON.parse(await readFile(resolve(scriptDirectory, "../package.json"), "utf8"));

describe("locale key parity", () => {
  it("runs locale parity as part of the standard check script", async () => {
    const packageJson = await readPackage();

    expect(packageJson.scripts.check).toContain("pnpm locale:check");
  });

  it("ignores translated values when key trees match", async () => {
    const english = await readFixture("equal", "en-US");
    const vietnamese = await readFixture("equal", "vi-VN");

    expect(compareLocaleKeys(english, vietnamese)).toEqual({ missing: [], extra: [] });
  });

  it("reports the exact dotted path missing from the translated catalog", async () => {
    const english = await readFixture("missing", "en-US");
    const vietnamese = await readFixture("missing", "vi-VN");

    expect(compareLocaleKeys(english, vietnamese)).toEqual({
      missing: ["nested.greeting.formal"],
      extra: [],
    });
    const result = spawnSync(process.execPath, [checkerPath, fixturePath("missing", "en-US"), fixturePath("missing", "vi-VN")], {
      encoding: "utf8",
    });
    expect(result.status).toBe(1);
    expect(`${result.stdout}${result.stderr}`).toContain("- nested.greeting.formal");
  });

  it("reports the exact dotted path extra in the translated catalog", async () => {
    const english = await readFixture("extra", "en-US");
    const vietnamese = await readFixture("extra", "vi-VN");

    expect(compareLocaleKeys(english, vietnamese)).toEqual({
      missing: [],
      extra: ["nested.greeting.informal"],
    });
    const result = spawnSync(process.execPath, [checkerPath, fixturePath("extra", "en-US"), fixturePath("extra", "vi-VN")], {
      encoding: "utf8",
    });
    expect(result.status).toBe(1);
    expect(`${result.stdout}${result.stderr}`).toContain("- nested.greeting.informal");
  });
});
