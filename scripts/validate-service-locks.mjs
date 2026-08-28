#!/usr/bin/env node

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

export const SERVICE_LOCK_TARGETS = Object.freeze([
  Object.freeze({
    target: "windows-cpython-311-x86_64",
    python: "3.11",
    relativePath: "install/locks/windows-cpython-311-x86_64.txt",
  }),
  Object.freeze({
    target: "windows-cpython-314-x86_64",
    python: "3.14",
    relativePath: "install/locks/windows-cpython-314-x86_64.txt",
  }),
]);

export const SERVICE_BOOTSTRAP_REQUIREMENTS = Object.freeze([
  Object.freeze(["pip", "26.2.1"]),
  Object.freeze(["setuptools", "84.0.0"]),
]);

const LOCK_TITLE = "# Hermes Document Reader service dependency lock";
const LOCK_SOURCE =
  "# sources: install/service-requirements.txt + scripts/lock-inputs/service-bootstrap.txt";
const LOCK_GENERATOR =
  "# generator: uv 0.12.3; uv pip compile --generate-hashes --only-binary=:all:";
const LOCK_INSTALL =
  "# install: python -m pip install --require-hashes --only-binary=:all: --requirement <this-file>";
const EXACT_PIN = /^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s;@\\]+)$/;
const LOCK_ENTRY =
  /^([a-z0-9][a-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*)\s+((?:--hash=sha256:[0-9a-f]{64})(?:\s+--hash=sha256:[0-9a-f]{64})*)$/;
const HASH = /^--hash=sha256:([0-9a-f]{64})$/;

function normalizeNewlines(value) {
  return String(value).replace(/\r\n?/g, "\n");
}

function canonicalName(value) {
  return value.toLowerCase().replace(/[-_.]+/g, "-");
}

export function parseExactRequirements(raw, label = "requirements") {
  const pins = new Map();
  for (const [index, sourceLine] of normalizeNewlines(raw).split("\n").entries()) {
    const line = sourceLine.trim();
    if (!line || line.startsWith("#")) continue;
    const match = line.match(EXACT_PIN);
    assert.ok(match, `${label}:${index + 1} must be one unconditional exact name==version pin`);
    const name = canonicalName(match[1]);
    assert.ok(!pins.has(name), `${label} repeats ${name}`);
    pins.set(name, match[2]);
  }
  assert.ok(pins.size > 0, `${label} must contain at least one exact pin`);
  return pins;
}

function logicalLockEntries(lines, label) {
  const entries = [];
  let current = "";
  for (let index = 5; index < lines.length; index += 1) {
    const trimmed = lines[index].trim();
    if (!trimmed || trimmed.startsWith("#")) {
      assert.equal(current, "", `${label}:${index + 1} interrupts a continued requirement`);
      continue;
    }
    const continued = trimmed.endsWith("\\");
    const fragment = continued ? trimmed.slice(0, -1).trimEnd() : trimmed;
    current = current ? `${current} ${fragment}` : fragment;
    if (!continued) {
      entries.push({ line: index + 1, value: current });
      current = "";
    }
  }
  assert.equal(current, "", `${label} ends with an unfinished line continuation`);
  return entries;
}

export function validateServiceLockText(raw, contract, requiredRequirements) {
  const label = contract.relativePath || contract.target;
  const normalized = normalizeNewlines(raw);
  assert.ok(normalized.endsWith("\n"), `${label} must end with a newline`);
  const lines = normalized.split("\n");
  assert.equal(lines[0], LOCK_TITLE, `${label} has the wrong lock title`);
  assert.equal(lines[1], `# target: ${contract.target}`, `${label} has the wrong target`);
  assert.equal(lines[2], LOCK_SOURCE, `${label} has the wrong source contract`);
  assert.equal(lines[3], LOCK_GENERATOR, `${label} has the wrong generator contract`);
  assert.equal(lines[4], LOCK_INSTALL, `${label} has the wrong install contract`);

  const packages = new Map();
  for (const entry of logicalLockEntries(lines, label)) {
    const match = entry.value.match(LOCK_ENTRY);
    assert.ok(
      match,
      `${label}:${entry.line} must be an exact package pin followed only by SHA-256 hashes`,
    );
    const name = canonicalName(match[1]);
    assert.equal(match[1], name, `${label}:${entry.line} package names must be canonical`);
    assert.ok(!packages.has(name), `${label} repeats ${name}`);
    const hashes = match[3].split(/\s+/).map((value) => {
      const hashMatch = value.match(HASH);
      assert.ok(hashMatch, `${label}:${entry.line} has an invalid hash`);
      return hashMatch[1];
    });
    assert.equal(new Set(hashes).size, hashes.length, `${label} repeats a hash for ${name}`);
    packages.set(name, Object.freeze({ version: match[2], hashes: Object.freeze(hashes) }));
  }

  const names = [...packages.keys()];
  assert.deepEqual(names, [...names].sort(), `${label} package pins must be sorted`);
  assert.ok(packages.size > requiredRequirements.size, `${label} is not a transitive lock`);
  for (const [name, version] of requiredRequirements) {
    assert.equal(
      packages.get(name)?.version,
      version,
      `${label} does not satisfy required service pin ${name}==${version}`,
    );
  }
  assert.equal(packages.get("pip")?.version, "26.2.1", `${label} must hash pip==26.2.1`);
  assert.equal(
    packages.get("setuptools")?.version,
    "84.0.0",
    `${label} must hash setuptools==84.0.0`,
  );
  return packages;
}

export function validateServiceLocks({ root = process.cwd() } = {}) {
  const resolvedRoot = path.resolve(root);
  const directRequirements = parseExactRequirements(
    fs.readFileSync(path.join(resolvedRoot, "install", "service-requirements.txt"), "utf8"),
    "install/service-requirements.txt",
  );
  const bootstrapRequirements = parseExactRequirements(
    fs.readFileSync(
      path.join(resolvedRoot, "scripts", "lock-inputs", "service-bootstrap.txt"),
      "utf8",
    ),
    "scripts/lock-inputs/service-bootstrap.txt",
  );
  assert.deepEqual(
    [...bootstrapRequirements],
    SERVICE_BOOTSTRAP_REQUIREMENTS,
    "service bootstrap inputs must remain the reviewed pip/setuptools pins",
  );
  const requiredRequirements = new Map(directRequirements);
  for (const [name, version] of bootstrapRequirements) {
    assert.ok(!requiredRequirements.has(name), `service lock input repeats ${name}`);
    requiredRequirements.set(name, version);
  }
  const releaseManifest = JSON.parse(
    fs.readFileSync(path.join(resolvedRoot, "scripts", "plugin-release-files.json"), "utf8"),
  );
  assert.ok(Array.isArray(releaseManifest.files), "plugin release allowlist is missing files");
  const allowlist = new Set(releaseManifest.files.map((value) => String(value).replaceAll("\\", "/")));
  const validated = new Map();
  for (const contract of SERVICE_LOCK_TARGETS) {
    assert.ok(allowlist.has(contract.relativePath), `${contract.relativePath} is not archive-allowlisted`);
    const raw = fs.readFileSync(path.join(resolvedRoot, contract.relativePath), "utf8");
    validated.set(contract.target, validateServiceLockText(raw, contract, requiredRequirements));
  }
  return Object.freeze({
    bootstrapRequirements,
    directRequirements,
    requiredRequirements,
    locks: validated,
  });
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) {
  try {
    const result = validateServiceLocks();
    process.stdout.write(
      `Validated ${result.locks.size} hashed Windows service locks from ${result.directRequirements.size} direct pins plus ${result.bootstrapRequirements.size} bootstrap pins.\n`,
    );
  } catch (error) {
    console.error(error instanceof Error ? error.message : "Service lock validation failed");
    process.exit(1);
  }
}
