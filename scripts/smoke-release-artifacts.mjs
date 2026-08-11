#!/usr/bin/env node

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

import { parse as parseYaml } from "yaml";

import { verifyReleaseMetadata } from "./verify-release-metadata.mjs";
import {
  SERVICE_BOOTSTRAP_REQUIREMENTS,
  SERVICE_LOCK_TARGETS,
  parseExactRequirements,
  validateServiceLockText,
} from "./validate-service-locks.mjs";

function argument(name) {
  const index = process.argv.indexOf(name);
  return index === -1 ? "" : process.argv[index + 1] || "";
}

function git(root, args) {
  return execFileSync("git", args, {
    cwd: root,
    encoding: "utf8",
    maxBuffer: 32 * 1024 * 1024,
    stdio: ["ignore", "pipe", "pipe"],
  }).trim();
}

function listZip(file) {
  return execFileSync("tar", ["-tf", file], { encoding: "utf8" })
    .split(/\r?\n/)
    .map((entry) => entry.replaceAll("\\", "/").replace(/^\.\//, ""))
    .filter((entry) => entry && !entry.endsWith("/"));
}

function archiveText(file, entry) {
  return execFileSync("tar", ["-xOf", file, entry], {
    encoding: "utf8",
    maxBuffer: 8 * 1024 * 1024,
  });
}

function verifyChecksum(file) {
  const expected = fs.readFileSync(`${file}.sha256`, "ascii").trim();
  const digest = createHash("sha256").update(fs.readFileSync(file)).digest("hex");
  assert.equal(expected, `${digest}  ${path.basename(file)}`);
}

function pythonConstant(source, name) {
  const escapedName = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = source.match(
    new RegExp(
      `^${escapedName}\\s*=\\s*(?:["']([^"']+)["']|([0-9]+))\\s*(?:#.*)?\\r?$`,
      "m",
    ),
  );
  assert.ok(match, `Missing literal Python constant ${name}`);
  return match[1] || match[2];
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

function pluginReleaseFiles(root) {
  const parsed = JSON.parse(
    fs.readFileSync(path.join(root, "scripts", "plugin-release-files.json"), "utf8"),
  );
  assert.ok(Array.isArray(parsed.files) && parsed.files.length > 0);
  const files = parsed.files.map((file) => String(file).replaceAll("\\", "/"));
  assert.equal(new Set(files).size, files.length, "Plugin allowlist must not contain duplicates");
  assert.deepEqual(files, [...files].sort(), "Plugin allowlist paths must be sorted");
  return files;
}

const FORBIDDEN_ARCHIVE_PATHS = [
  /(^|\/)\.env(?:\.[^/]*)?$/i,
  /(^|\/)(?:backups|data|history|inbox|jobs|logs|needs-review|node_modules|on-hold|onhold|processed|quarantine|receipts|retry|runtime|state|uploads|\.test-tmp|\.venv|venv|__pycache__|\.pytest_cache)(?:\/|$)/i,
  /^(?:dist|hermes-document-reader-source-v[^/]+\/dist)(?:\/|$)/i,
  /(^|\/)pytest-cache-files-[^/]+(?:\/|$)/i,
  /(^|\/)(?:history\.json|service\.token|[^/]*receipt[^/]*|ownership[^/]*)$/i,
  /(^|\/)(?:\.?(?:api|auth|access|refresh)[-_.]?token|\.?tokens?)(?:\.[^/]*)?$/i,
  /\.(?:log|pyc|pyo|pid|sock|token|key|pem|pfx|p12|jks)$/i,
];
const DOCUMENTATION_DIRECTORY = /(^|\/)docs(?:\/|$)/i;

const EXECUTABLE_PLACEHOLDERS = [
  /<hermes-home>/i,
  /http:\/\/your-ocr-host/i,
  /http:\/\/your-vllm-host/i,
  /C:\/Users\/youruser/i,
];

const HIGH_CONFIDENCE_SECRETS = [
  /-----BEGIN (?:EC |OPENSSH |RSA )?PRIVATE KEY-----/,
  /\bgh[opsu]_[A-Za-z0-9]{30,}\b/,
  /\bgithub_pat_[A-Za-z0-9_]{50,}\b/,
  /\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b/,
  /\b\d{6,12}:[A-Za-z0-9_-]{30,}\b/,
];
const SOURCE_ONLY_RUNTIME_FILES = Object.freeze([
  "mcp/anydoc-mcp.py",
  "service/install-autostart.ps1",
  "viewer/index.html",
  "viewer/viewer.py",
]);

export function assertArchiveHygiene(entries, { allowDocumentation = false } = {}) {
  const violations = entries.filter(
    (entry) =>
      FORBIDDEN_ARCHIVE_PATHS.some((pattern) => pattern.test(entry)) ||
      (!allowDocumentation && DOCUMENTATION_DIRECTORY.test(entry)),
  );
  assert.deepEqual(violations, [], `Forbidden release paths:\n${violations.join("\n")}`);
}

export function assertNoExecutablePlaceholders(archive, entries) {
  const executableEntries = entries.filter((entry) => /\.(?:html|js|mjs|ps1|py)$/i.test(entry));
  const violations = [];
  for (const entry of executableEntries) {
    const content = archiveText(archive, entry).replaceAll("\\", "/");
    for (const pattern of EXECUTABLE_PLACEHOLDERS) {
      if (pattern.test(content)) violations.push(`${entry}: ${pattern}`);
    }
  }
  assert.deepEqual(
    violations,
    [],
    `Executable release files contain deployment placeholders:\n${violations.join("\n")}`,
  );
}

export function assertNoHighConfidenceSecrets(archive, entries) {
  const violations = [];
  for (const entry of entries) {
    const content = archiveText(archive, entry);
    for (const pattern of HIGH_CONFIDENCE_SECRETS) {
      if (pattern.test(content)) violations.push(`${entry}: ${pattern}`);
    }
  }
  assert.deepEqual(
    violations,
    [],
    `Release files contain high-confidence secret material:\n${violations.join("\n")}`,
  );
}

function smokePluginSyntax(pluginPath) {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "document-reader-plugin-smoke-"));
  try {
    execFileSync("tar", ["-xf", pluginPath, "-C", temporary], { stdio: "pipe" });
    for (const relativePath of [
      "dashboard/dist/index.js",
      "desktop-plugin/document-reader/plugin.js",
    ]) {
      execFileSync(process.execPath, ["--check", path.join(temporary, relativePath)], {
        stdio: "pipe",
      });
    }
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}

export function smokeReleaseArtifacts({
  root = process.cwd(),
  outputDir = "dist",
  treeish = "HEAD",
  version = "",
} = {}) {
  const resolvedRoot = path.resolve(root);
  const metadataVersion = verifyReleaseMetadata({ root: resolvedRoot }).version;
  const releaseVersion = version || metadataVersion;
  assert.equal(
    releaseVersion,
    metadataVersion,
    "Requested artifact version must match synchronized release metadata",
  );

  const resolvedOutput = path.resolve(resolvedRoot, outputDir);
  const productPath = path.join(
    resolvedOutput,
    `hermes-document-reader-source-v${releaseVersion}.zip`,
  );
  const pluginPath = path.join(resolvedOutput, `hermes-document-reader-v${releaseVersion}.zip`);
  for (const file of [productPath, pluginPath, `${productPath}.sha256`, `${pluginPath}.sha256`]) {
    assert.ok(fs.existsSync(file), `Missing release artifact: ${file}`);
  }
  verifyChecksum(productPath);
  verifyChecksum(pluginPath);

  const tracked = git(resolvedRoot, ["ls-tree", "-r", "--name-only", treeish])
    .split(/\r?\n/)
    .filter(Boolean)
    .map((entry) => entry.replaceAll("\\", "/"));
  const productPrefix = `hermes-document-reader-source-v${releaseVersion}/`;
  const expectedProduct = tracked.map((entry) => `${productPrefix}${entry}`).sort();
  const productEntries = listZip(productPath).sort();
  assert.deepEqual(
    productEntries,
    expectedProduct,
    "Whole-product ZIP must match the tracked Git tree exactly",
  );

  const expectedPlugin = pluginReleaseFiles(resolvedRoot);
  const pluginEntries = listZip(pluginPath).sort();
  assert.deepEqual(
    pluginEntries,
    expectedPlugin,
    "Plugin ZIP must match the explicit installable-file allowlist at archive root",
  );
  for (const required of [
    "__init__.py",
    "plugin.yaml",
    "LICENSE",
    "desktop-plugin/document-reader/plugin.js",
    "install/locks/windows-cpython-311-x86_64.txt",
    "install/locks/windows-cpython-314-x86_64.txt",
  ]) {
    assert.ok(pluginEntries.includes(required), `Plugin archive is missing ${required}`);
  }

  assertArchiveHygiene(productEntries, { allowDocumentation: true });
  assertArchiveHygiene(pluginEntries);
  for (const sourceOnly of SOURCE_ONLY_RUNTIME_FILES) {
    assert.ok(tracked.includes(sourceOnly), `Whole-product runtime scan is missing ${sourceOnly}`);
  }
  const sourceRuntimeEntries = [...expectedPlugin, ...SOURCE_ONLY_RUNTIME_FILES]
    .map((entry) => `${productPrefix}${entry}`)
    .sort();
  assertNoExecutablePlaceholders(productPath, sourceRuntimeEntries);
  assertNoHighConfidenceSecrets(productPath, sourceRuntimeEntries);
  assertNoExecutablePlaceholders(pluginPath, pluginEntries);
  assertNoHighConfidenceSecrets(pluginPath, pluginEntries);

  const productPackage = JSON.parse(archiveText(productPath, `${productPrefix}package.json`));
  assert.equal(productPackage.version, releaseVersion);
  const pluginManifest = archiveText(pluginPath, "plugin.yaml");
  const parsedManifest = parseYaml(pluginManifest);
  assert.ok(parsedManifest && typeof parsedManifest === "object" && !Array.isArray(parsedManifest));
  assert.equal(String(parsedManifest.version), releaseVersion);
  assert.equal(parsedManifest.name, "document-reader");
  const dashboardManifest = JSON.parse(archiveText(pluginPath, "dashboard/manifest.json"));
  assert.equal(String(dashboardManifest.version), releaseVersion);
  assert.equal(dashboardManifest.name, "document-reader");
  assert.equal(dashboardManifest.entry, "dist/index.js");
  assert.equal(dashboardManifest.api, "plugin_api.py");
  const pluginRuntime = archiveText(pluginPath, "desktop-plugin/document-reader/plugin.js");
  assert.equal(desktopVersion(pluginRuntime), releaseVersion);
  const profileRuntime = archiveText(pluginPath, "profile_runtime.py");
  const serviceRuntime = archiveText(pluginPath, "service/ocr_service.py");
  assert.equal(pythonConstant(profileRuntime, "PLUGIN_VERSION"), releaseVersion);
  assert.equal(pythonConstant(serviceRuntime, "VERSION"), releaseVersion);
  assert.equal(
    pythonConstant(profileRuntime, "SERVICE_API_VERSION"),
    pythonConstant(serviceRuntime, "API_VERSION"),
  );
  const serviceRequirements = parseExactRequirements(
    archiveText(pluginPath, "install/service-requirements.txt"),
    "install/service-requirements.txt in plugin archive",
  );
  for (const [name, version] of SERVICE_BOOTSTRAP_REQUIREMENTS) {
    assert.ok(!serviceRequirements.has(name), `service lock input repeats ${name}`);
    serviceRequirements.set(name, version);
  }
  for (const contract of SERVICE_LOCK_TARGETS) {
    validateServiceLockText(
      archiveText(pluginPath, contract.relativePath),
      contract,
      serviceRequirements,
    );
  }
  smokePluginSyntax(pluginPath);

  return { productPath, pluginPath, version: releaseVersion, treeish };
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) {
  try {
    const result = smokeReleaseArtifacts({
      outputDir: argument("--output-dir") || "dist",
      treeish: argument("--tree") || process.env.RELEASE_TREEISH || "HEAD",
      version: argument("--version"),
    });
    process.stdout.write(
      `Verified ${path.basename(result.productPath)} and ${path.basename(result.pluginPath)} against ${result.treeish}\n`,
    );
  } catch (error) {
    console.error(error instanceof Error ? error.message : "Release artifact smoke test failed");
    process.exit(1);
  }
}
