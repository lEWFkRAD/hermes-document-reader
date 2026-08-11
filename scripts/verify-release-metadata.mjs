#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

import { parse as parseYaml } from "yaml";

const SEMVER = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/;

function fail(message) {
  throw new Error(message);
}

function read(root, relativePath, integrationHint = "") {
  const file = path.join(root, relativePath);
  if (!fs.existsSync(file)) {
    const suffix = integrationHint ? ` ${integrationHint}` : "";
    fail(`${relativePath} is required for release validation.${suffix}`);
  }
  return fs.readFileSync(file, "utf8");
}

function parseManifest(source) {
  let parsed;
  try {
    parsed = parseYaml(source);
  } catch (error) {
    fail(`plugin.yaml is invalid YAML: ${error.message}`);
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    fail("plugin.yaml must contain a YAML mapping");
  }
  return parsed;
}

function parseJson(source, relativePath) {
  try {
    const parsed = JSON.parse(source);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      fail(`${relativePath} must contain a JSON object`);
    }
    return parsed;
  } catch (error) {
    fail(`${relativePath} is invalid JSON: ${error.message}`);
  }
}

function desktopVersion(source) {
  const constants = new Map();
  for (const match of source.matchAll(
    /^(?:export\s+)?const\s+([A-Z][A-Z0-9_]*)\s*=\s*["']([^"']+)["'];?\s*$/gm,
  )) {
    constants.set(match[1], match[2]);
  }
  const literal = source.match(/\bversion\s*:\s*["']([^"']+)["']/)?.[1];
  if (literal) return literal;
  const reference = source.match(/\bversion\s*:\s*([A-Z][A-Z0-9_]*)\b/)?.[1];
  return reference ? constants.get(reference) || "" : "";
}

function pythonConstant(source, name) {
  const escapedName = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = source.match(
    new RegExp(
      `^${escapedName}\\s*=\\s*(?:["']([^"']+)["']|([0-9]+))\\s*(?:#.*)?\\r?$`,
      "m",
    ),
  );
  return match?.[1] || match?.[2] || "";
}

export function verifyReleaseMetadata({ root = process.cwd(), tag = "" } = {}) {
  const packageMetadata = JSON.parse(read(root, "package.json"));
  const packageVersion = packageMetadata.version;
  if (!SEMVER.test(packageVersion)) {
    fail(`package.json has an invalid SemVer version: ${packageVersion}`);
  }
  if (packageMetadata.name !== "hermes-document-reader") {
    fail(`package.json name must be hermes-document-reader, found ${packageMetadata.name}`);
  }

  const lockMetadata = JSON.parse(read(root, "package-lock.json"));
  const lockVersion = lockMetadata.version;
  const lockRootVersion = lockMetadata.packages?.[""]?.version;

  const manifest = read(
    root,
    "plugin.yaml",
    "Integrate the root Hermes plugin manifest before CI can pass.",
  );
  const parsedManifest = parseManifest(manifest);
  const pluginManifestVersion = String(parsedManifest.version || "");
  if (!pluginManifestVersion) fail("plugin.yaml does not declare a top-level version");
  if (parsedManifest.name !== "document-reader") {
    fail(`plugin.yaml name must be document-reader, found ${parsedManifest.name}`);
  }

  const dashboardManifest = parseJson(read(root, "dashboard/manifest.json"), "dashboard/manifest.json");
  if (dashboardManifest.name !== "document-reader") {
    fail(
      `dashboard/manifest.json name must be document-reader, found ${dashboardManifest.name}`,
    );
  }
  const dashboardVersion = String(dashboardManifest.version || "");
  if (!dashboardVersion) fail("dashboard/manifest.json does not declare a version");
  if (dashboardManifest.entry !== "dist/index.js") {
    fail("dashboard/manifest.json entry must be dist/index.js");
  }
  if (dashboardManifest.api !== "plugin_api.py") {
    fail("dashboard/manifest.json api must be plugin_api.py");
  }

  const desktop = read(root, "desktop-plugin/document-reader/plugin.js");
  const pluginRuntimeVersion = desktopVersion(desktop);
  if (!pluginRuntimeVersion) {
    fail(
      "desktop-plugin/document-reader/plugin.js must export a literal version or a version constant",
    );
  }

  const profileRuntime = read(root, "profile_runtime.py");
  const profileRuntimeVersion = pythonConstant(profileRuntime, "PLUGIN_VERSION");
  const profileApiVersion = pythonConstant(profileRuntime, "SERVICE_API_VERSION");
  if (!profileRuntimeVersion) fail("profile_runtime.py must declare literal PLUGIN_VERSION");
  if (!profileApiVersion) fail("profile_runtime.py must declare integer SERVICE_API_VERSION");

  const serviceRuntime = read(root, "service/ocr_service.py");
  const serviceRuntimeVersion = pythonConstant(serviceRuntime, "VERSION");
  const serviceApiVersion = pythonConstant(serviceRuntime, "API_VERSION");
  if (!serviceRuntimeVersion) fail("service/ocr_service.py must declare literal VERSION");
  if (!serviceApiVersion) fail("service/ocr_service.py must declare integer API_VERSION");
  if (profileApiVersion !== serviceApiVersion) {
    fail(
      `Service API version mismatch: profile_runtime.py=${profileApiVersion}; service/ocr_service.py=${serviceApiVersion}`,
    );
  }

  const versions = {
    "package.json": packageVersion,
    "package-lock.json": lockVersion,
    "package-lock.json root package": lockRootVersion,
    "plugin.yaml": pluginManifestVersion,
    "dashboard/manifest.json": dashboardVersion,
    "desktop-plugin/document-reader/plugin.js": pluginRuntimeVersion,
    "profile_runtime.py": profileRuntimeVersion,
    "service/ocr_service.py": serviceRuntimeVersion,
  };
  const mismatches = Object.entries(versions)
    .filter(([, version]) => version !== packageVersion)
    .map(([source, version]) => `${source}=${version || "missing"}`);
  if (mismatches.length) {
    fail(`Release version mismatch: package.json=${packageVersion}; ${mismatches.join("; ")}`);
  }

  const changelog = read(root, "CHANGELOG.md");
  const escapedVersion = packageVersion.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const releaseHeading = changelog.match(
    new RegExp(
      `^## \\[${escapedVersion}\\] - (Unreleased|\\d{4}-\\d{2}-\\d{2})\\r?$`,
      "m",
    ),
  );
  if (!releaseHeading) {
    fail(
      `CHANGELOG.md must contain "## [${packageVersion}] - Unreleased" or a dated release heading`,
    );
  }

  if (tag) {
    if (tag !== `v${packageVersion}`) {
      fail(`Tag ${tag} does not match release version v${packageVersion}`);
    }
    if (releaseHeading[1] === "Unreleased") {
      fail(`CHANGELOG.md must date the ${packageVersion} section before tag ${tag} can be released`);
    }
  }

  return { version: packageVersion, changelogState: releaseHeading[1] };
}

function argument(name) {
  const index = process.argv.indexOf(name);
  return index === -1 ? "" : process.argv[index + 1] || "";
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) {
  try {
    const result = verifyReleaseMetadata({ tag: argument("--tag") });
    process.stdout.write(`${result.version}\n`);
  } catch (error) {
    console.error(error instanceof Error ? error.message : "Release metadata validation failed");
    process.exit(1);
  }
}
