import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const defaultEnglishPath = resolve(scriptDirectory, "../src/locales/en-US/main.json");
const defaultVietnamesePath = resolve(scriptDirectory, "../src/locales/vi-VN/main.json");

function collectLeafPaths(value, prefix = "", paths = new Set()) {
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    const entries = Object.entries(value);
    if (entries.length === 0 && prefix) paths.add(prefix);
    for (const [key, child] of entries) {
      const path = prefix ? `${prefix}.${key}` : key;
      collectLeafPaths(child, path, paths);
    }
    return paths;
  }

  if (prefix) paths.add(prefix);
  return paths;
}

export function compareLocaleKeys(englishCatalog, translatedCatalog) {
  const englishPaths = collectLeafPaths(englishCatalog);
  const translatedPaths = collectLeafPaths(translatedCatalog);

  return {
    missing: [...englishPaths].filter((path) => !translatedPaths.has(path)).sort(),
    extra: [...translatedPaths].filter((path) => !englishPaths.has(path)).sort(),
  };
}

export async function checkLocaleParity(
  englishPath = defaultEnglishPath,
  translatedPath = defaultVietnamesePath,
) {
  const [english, translated] = await Promise.all([
    readFile(englishPath, "utf8").then(JSON.parse),
    readFile(translatedPath, "utf8").then(JSON.parse),
  ]);
  return compareLocaleKeys(english, translated);
}

function printDifferences({ missing, extra }) {
  if (missing.length === 0 && extra.length === 0) {
    console.log("Locale key parity check passed.");
    return;
  }

  console.error("Locale key parity check failed.");
  if (missing.length > 0) {
    console.error("Missing from translated catalog:");
    for (const path of missing) console.error(`- ${path}`);
  }
  if (extra.length > 0) {
    console.error("Extra in translated catalog:");
    for (const path of extra) console.error(`- ${path}`);
  }
}

const isMain = process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url;
if (isMain) {
  const [englishPath = defaultEnglishPath, translatedPath = defaultVietnamesePath] = process.argv.slice(2);
  try {
    const differences = await checkLocaleParity(englishPath, translatedPath);
    printDifferences(differences);
    if (differences.missing.length > 0 || differences.extra.length > 0) process.exitCode = 1;
  } catch (error) {
    console.error(`Locale key parity check could not read catalogs: ${error.message}`);
    process.exitCode = 1;
  }
}
