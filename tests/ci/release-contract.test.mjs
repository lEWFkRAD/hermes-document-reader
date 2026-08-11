import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { parse as parseYaml } from "yaml";

import { buildReleaseArtifacts } from "../../scripts/build-release-artifacts.mjs";
import { verifyReleaseAncestry } from "../../scripts/verify-release-ancestry.mjs";
import { verifyReleaseMetadata } from "../../scripts/verify-release-metadata.mjs";
import { verifyReleaseTag } from "../../scripts/verify-release-tag.mjs";
import { assertArchiveHygiene } from "../../scripts/smoke-release-artifacts.mjs";
import {
  SERVICE_BOOTSTRAP_REQUIREMENTS,
  SERVICE_LOCK_TARGETS,
  parseExactRequirements,
  validateServiceLockText,
  validateServiceLocks,
} from "../../scripts/validate-service-locks.mjs";

const VERSION = "0.1.0";

async function metadataFixture({
  manifest = `manifest_version: 1\nname: document-reader\nversion: ${VERSION}\nkind: standalone\n`,
  dashboard = {
    name: "document-reader",
    version: VERSION,
    entry: "dist/index.js",
    api: "plugin_api.py",
  },
  desktop = `const PLUGIN_VERSION = '${VERSION}'\nexport default { id: 'document-reader', version: PLUGIN_VERSION }\n`,
  profileRuntime = `PLUGIN_VERSION = "${VERSION}"\nSERVICE_API_VERSION = 1\n`,
  serviceRuntime = `VERSION = "${VERSION}"\nAPI_VERSION = 1\n`,
  changelog = `# Changelog\n\n## [${VERSION}] - Unreleased\n`,
} = {}) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "document-reader-metadata-"));
  await fs.mkdir(path.join(root, "desktop-plugin", "document-reader"), { recursive: true });
  await fs.mkdir(path.join(root, "dashboard"), { recursive: true });
  await fs.mkdir(path.join(root, "service"), { recursive: true });
  await fs.writeFile(
    path.join(root, "package.json"),
    `${JSON.stringify({ name: "hermes-document-reader", version: VERSION })}\n`,
  );
  await fs.writeFile(
    path.join(root, "package-lock.json"),
    `${JSON.stringify({ version: VERSION, packages: { "": { version: VERSION } } })}\n`,
  );
  if (manifest !== null) await fs.writeFile(path.join(root, "plugin.yaml"), manifest);
  if (dashboard !== null) {
    await fs.writeFile(path.join(root, "dashboard", "manifest.json"), `${JSON.stringify(dashboard)}\n`);
  }
  await fs.writeFile(
    path.join(root, "desktop-plugin", "document-reader", "plugin.js"),
    desktop,
  );
  await fs.writeFile(path.join(root, "profile_runtime.py"), profileRuntime);
  await fs.writeFile(path.join(root, "service", "ocr_service.py"), serviceRuntime);
  await fs.writeFile(path.join(root, "CHANGELOG.md"), changelog);
  return root;
}

test("release metadata synchronizes package, manifest, and desktop plugin versions", () => {
  const result = verifyReleaseMetadata();
  assert.equal(result.version, VERSION);
  assert.equal(result.changelogState, "Unreleased");
});

test("the current Unreleased version cannot be validated as a release tag", () => {
  assert.throws(
    () => verifyReleaseMetadata({ tag: `v${VERSION}` }),
    /must date the 0\.1\.0 section before tag v0\.1\.0 can be released/,
  );
});

test("npm lockfile root metadata matches the canonical version", async () => {
  const packageMetadata = JSON.parse(await fs.readFile("package.json", "utf8"));
  const lockMetadata = JSON.parse(await fs.readFile("package-lock.json", "utf8"));
  assert.equal(lockMetadata.version, packageMetadata.version);
  assert.equal(lockMetadata.packages[""].version, packageMetadata.version);
});

test("metadata validator consumes a desktop version constant", async () => {
  const root = await metadataFixture();
  try {
    assert.deepEqual(verifyReleaseMetadata({ root }), {
      version: VERSION,
      changelogState: "Unreleased",
    });
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("release policy parsers accept CRLF-normalized metadata", async () => {
  const root = await metadataFixture({
    manifest: `manifest_version: 1\r\nname: document-reader\r\nversion: ${VERSION}\r\nkind: standalone\r\n`,
    desktop: `const PLUGIN_VERSION = '${VERSION}'\r\nexport default { id: 'document-reader', version: PLUGIN_VERSION }\r\n`,
    profileRuntime: `PLUGIN_VERSION = "${VERSION}"\r\nSERVICE_API_VERSION = 1\r\n`,
    serviceRuntime: `VERSION = "${VERSION}"\r\nAPI_VERSION = 1\r\n`,
    changelog: `# Changelog\r\n\r\n## [${VERSION}] - Unreleased\r\n`,
  });
  try {
    assert.deepEqual(verifyReleaseMetadata({ root }), {
      version: VERSION,
      changelogState: "Unreleased",
    });
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("metadata validator clearly rejects a missing plugin manifest", async () => {
  const root = await metadataFixture({ manifest: null });
  try {
    assert.throws(
      () => verifyReleaseMetadata({ root }),
      /plugin\.yaml is required.*Integrate the root Hermes plugin manifest/,
    );
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("metadata validator clearly rejects missing desktop version metadata", async () => {
  const root = await metadataFixture({
    desktop: "export default { id: 'document-reader', name: 'Document Reader' }\n",
  });
  try {
    assert.throws(
      () => verifyReleaseMetadata({ root }),
      /plugin\.js must export a literal version or a version constant/,
    );
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("metadata validator rejects dashboard version drift", async () => {
  const root = await metadataFixture({
    dashboard: {
      name: "document-reader",
      version: "9.9.9",
      entry: "dist/index.js",
      api: "plugin_api.py",
    },
  });
  try {
    assert.throws(
      () => verifyReleaseMetadata({ root }),
      /dashboard\/manifest\.json=9\.9\.9/,
    );
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("metadata validator rejects Python runtime version drift", async () => {
  const root = await metadataFixture({
    profileRuntime: `PLUGIN_VERSION = "9.9.9"\nSERVICE_API_VERSION = 1\n`,
  });
  try {
    assert.throws(() => verifyReleaseMetadata({ root }), /profile_runtime\.py=9\.9\.9/);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("metadata validator rejects service API version drift", async () => {
  const root = await metadataFixture({
    serviceRuntime: `VERSION = "${VERSION}"\nAPI_VERSION = 2\n`,
  });
  try {
    assert.throws(
      () => verifyReleaseMetadata({ root }),
      /Service API version mismatch: profile_runtime\.py=1; service\/ocr_service\.py=2/,
    );
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("release builder rejects a caller-supplied version mismatch", () => {
  assert.throws(
    () => buildReleaseArtifacts({ version: "99.99.99" }),
    /does not match metadata version/,
  );
});

test("runtime and development requirements are exact reviewed pins", async () => {
  const runtime = (await fs.readFile("requirements.txt", "utf8"))
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"));
  const development = (await fs.readFile("requirements-dev.txt", "utf8"))
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"));
  const service = (await fs.readFile("install/service-requirements.txt", "utf8"))
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"));

  for (const requirement of [...runtime, ...development, ...service]) {
    assert.match(requirement, /^[A-Za-z0-9_.-]+==[^\s;]+(?:;.+)?$/);
  }
  for (const requirement of service) {
    assert.ok(
      runtime.includes(requirement),
      `install/service-requirements.txt must match the reviewed root pin: ${requirement}`,
    );
  }
  assert.ok(
    runtime.some((entry) => entry.toLowerCase() === "firecrawl-anydoc==0.1.2"),
    "requirements.txt must install the anydoc module imported by the MCP server",
  );
  assert.ok(
    runtime.includes("fastapi==0.133.1"),
    "requirements.txt must pin the dashboard API framework",
  );
  assert.ok(
    runtime.includes("python-multipart==0.0.32"),
    "requirements.txt must directly pin dashboard upload parsing",
  );
  for (const required of ["pip", "pip-audit", "pytest", "setuptools"]) {
    assert.ok(
      development.some((entry) => entry.toLowerCase().startsWith(`${required}==`)),
      `requirements-dev.txt must pin ${required}`,
    );
  }
  assert.ok(development.includes("pip==26.1.2"), "CI bootstrap pip pin must be explicit");
  assert.ok(development.includes("PyYAML==6.0.3"), "archive YAML parsing must be pinned");
  assert.ok(
    development.includes("setuptools==83.0.0"),
    "the fully resolved environment must include the reviewed setuptools security fix",
  );
});

test("Windows service locks are complete, hashed, lane-bound, and archive-allowlisted", () => {
  const result = validateServiceLocks();
  assert.equal(result.directRequirements.size, 7);
  assert.deepEqual([...result.bootstrapRequirements], SERVICE_BOOTSTRAP_REQUIREMENTS);
  assert.equal(result.requiredRequirements.size, 9);
  assert.deepEqual([...result.locks.keys()], SERVICE_LOCK_TARGETS.map(({ target }) => target));
  for (const packages of result.locks.values()) {
    assert.equal(packages.size, 32);
    assert.equal(packages.get("pip").version, "26.1.2");
    assert.equal(packages.get("setuptools").version, "83.0.0");
    for (const requirement of packages.values()) assert.ok(requirement.hashes.length > 0);
  }
});

test("service lock regeneration has explicit resolver, target, and bootstrap inputs", async () => {
  const generator = await fs.readFile("scripts/regenerate-service-locks.ps1", "utf8");
  const bootstrap = parseExactRequirements(
    await fs.readFile("scripts/lock-inputs/service-bootstrap.txt", "utf8"),
    "scripts/lock-inputs/service-bootstrap.txt",
  );
  assert.deepEqual([...bootstrap], SERVICE_BOOTSTRAP_REQUIREMENTS);
  assert.match(generator, /uv 0\.12\.3/);
  assert.match(generator, /install\/service-requirements\.txt/);
  assert.match(generator, /lock-inputs\/service-bootstrap\.txt/);
  assert.match(generator, /--python-platform x86_64-pc-windows-msvc/);
  assert.match(generator, /--only-binary ":all:"/);
  assert.match(generator, /--generate-hashes/);
  assert.match(generator, /windows-cpython-311-x86_64/);
  assert.match(generator, /windows-cpython-314-x86_64/);
});

test("service lock parser tolerates CRLF but rejects target, pin, and hash drift", () => {
  const contract = {
    target: "windows-cpython-311-x86_64",
    relativePath: "fixture-lock.txt",
  };
  const direct = parseExactRequirements(
    "alpha==1.0\r\npip==26.1.2\r\nsetuptools==83.0.0\r\n",
    "fixture requirements",
  );
  const hashA = "a".repeat(64);
  const hashB = "b".repeat(64);
  const valid = [
    "# Hermes Document Reader service dependency lock",
    `# target: ${contract.target}`,
    "# sources: install/service-requirements.txt + scripts/lock-inputs/service-bootstrap.txt",
    "# generator: uv 0.12.3; uv pip compile --generate-hashes --only-binary=:all:",
    "# install: python -m pip install --require-hashes --only-binary=:all: --requirement <this-file>",
    "alpha==1.0 \\",
    `    --hash=sha256:${hashA}`,
    "bravo==2.0 \\",
    `    --hash=sha256:${"d".repeat(64)}`,
    "pip==26.1.2 \\",
    `    --hash=sha256:${hashB}`,
    "setuptools==83.0.0 \\",
    `    --hash=sha256:${"c".repeat(64)}`,
    "",
  ].join("\r\n");
  assert.equal(validateServiceLockText(valid, contract, direct).size, 4);
  assert.throws(
    () => validateServiceLockText(valid.replace(contract.target, "windows-cpython-314-x86_64"), contract, direct),
    /wrong target/,
  );
  assert.throws(
    () => validateServiceLockText(valid.replace("alpha==1.0", "alpha==2.0"), contract, direct),
    /does not satisfy required service pin/,
  );
  assert.throws(
    () => validateServiceLockText(valid.replace(hashA, "short"), contract, direct),
    /must be an exact package pin followed only by SHA-256 hashes/,
  );
});

test("Hermes manifest declares the standalone Windows plugin contract", async () => {
  const manifest = await fs.readFile("plugin.yaml", "utf8");
  const parsed = parseYaml(manifest);
  assert.equal(parsed.manifest_version, 1);
  assert.equal(parsed.name, "document-reader");
  assert.equal(String(parsed.version), VERSION);
  assert.equal(parsed.kind, "standalone");
  assert.ok(parsed.platforms.includes("windows"));
});

test("installable plugin allowlist is explicit, complete, and excludes repository machinery", async () => {
  const parsed = JSON.parse(await fs.readFile("scripts/plugin-release-files.json", "utf8"));
  assert.ok(Array.isArray(parsed.files));
  assert.deepEqual(parsed.files, [...parsed.files].sort());
  assert.equal(new Set(parsed.files).size, parsed.files.length);
  for (const required of [
    "LICENSE",
    "README.md",
    "__init__.py",
    "cli.py",
    "dashboard/dist/index.js",
    "dashboard/manifest.json",
    "dashboard/plugin_api.py",
    "desktop-plugin/document-reader/plugin.js",
    "engine/grm_ocr.py",
    "engine_config.py",
    "install/locks/windows-cpython-311-x86_64.txt",
    "install/locks/windows-cpython-314-x86_64.txt",
    "install/profile_service.py",
    "install/windows-task.ps1",
    "lifecycle.py",
    "plugin.yaml",
    "profile_runtime.py",
    "service/ocr_service.py",
  ]) {
    assert.ok(parsed.files.includes(required), `plugin release allowlist is missing ${required}`);
  }
  for (const file of parsed.files) {
    assert.doesNotMatch(file, /^(?:\.github|scripts|tests)(?:\/|$)/);
    assert.doesNotMatch(file, /(?:^|\/)(?:data|history|jobs|runtime|uploads)(?:\/|$)/i);
  }
  assert.ok(!parsed.files.includes("service/install-autostart.ps1"));
  assert.ok(!parsed.files.includes("requirements.txt"));
  assert.ok(!parsed.files.some((file) => file.startsWith("mcp/")));
  assert.ok(!parsed.files.some((file) => file.startsWith("viewer/")));
  for (const retired of [
    "install/install.ps1",
    "install/rollback.ps1",
    "install/status.ps1",
    "install/uninstall.ps1",
  ]) {
    assert.ok(!parsed.files.includes(retired), `${retired} must remain source-only`);
  }
  const gitignore = await fs.readFile(".gitignore", "utf8");
  assert.match(gitignore, /^\/dist\/$/m, "root release output must remain ignored");
  assert.doesNotMatch(
    gitignore,
    /^dist\/$/m,
    "dashboard/dist must remain eligible for the reviewed Git tree",
  );
});

test("installable plugin and public release docs contain no retired deployment identity", async () => {
  const parsed = JSON.parse(await fs.readFile("scripts/plugin-release-files.json", "utf8"));
  const publicFiles = new Set([
    ...parsed.files,
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "RELEASING.md",
    "SECURITY.md",
  ]);
  const retiredIdentity = /\b(?:Bearden|Onyx|forge\s*5090|OCR-Inbox)\b/i;
  for (const file of [...publicFiles].sort()) {
    const text = await fs.readFile(file, "utf8");
    assert.doesNotMatch(text, retiredIdentity, `${file} contains a retired deployment identity`);
  }
});

test("CI is fork-safe, SHA-pinned, and exposes only the aggregate PR context", async () => {
  const ci = await fs.readFile(".github/workflows/ci.yml", "utf8");
  const parsed = parseYaml(ci.replaceAll("\n", "\r\n"));
  assert.equal(parsed.permissions.contents, "read");
  assert.ok(parsed.jobs.required.needs.includes("package"));
  assert.equal(parsed.on.pull_request_target, undefined);
  assert.match(ci, /^permissions:\n\s+contents:\s+read\s*$/m);
  assert.match(ci, /'Required PR checks'/);
  assert.match(ci, /'CI summary'/);
  assert.match(ci, /expected_dco = "success" if .* == "pull_request" else "skipped"/);
  assert.match(ci, /python -m pip_audit --local/);
  assert.match(ci, /--basetemp/);
  assert.match(ci, /python -m venv \$serviceRoot/);
  assert.match(ci, /--require-hashes --only-binary=:all: --report \$report/);
  assert.match(ci, /verify_service_lock_install\.py/);
  assert.match(ci, /scripts\/verify-lifecycle-provisioning\.py/);
  assert.match(ci, /python-version: \["3\.11", "3\.14"\]/);

  const uses = [...ci.matchAll(/^\s*-?\s*uses:\s+([^\s#]+).*$/gm)].map((match) => match[1]);
  assert.ok(uses.length >= 8);
  for (const action of uses) assert.match(action, /^[^@\s]+@[0-9a-f]{40,64}$/);
});

test("repository governance covers dependencies and sensitive plugin surfaces", async () => {
  const dependabot = parseYaml(await fs.readFile(".github/dependabot.yml", "utf8"));
  const updates = dependabot.updates.map((entry) => `${entry["package-ecosystem"]}:${entry.directory}`);
  assert.deepEqual(updates, ["npm:/", "pip:/", "pip:/install", "github-actions:/"]);
  const owners = await fs.readFile(".github/CODEOWNERS", "utf8");
  for (const sensitive of [
    "/.github/",
    "/dashboard/",
    "/engine/",
    "/install/",
    "/lifecycle.py",
    "/profile_runtime.py",
    "/scripts/",
    "/service/",
  ]) {
    assert.match(owners, new RegExp(`^${sensitive.replaceAll("/", "\\/")} @lEWFkRAD$`, "m"));
  }
  const template = await fs.readFile(".github/PULL_REQUEST_TEMPLATE.md", "utf8");
  assert.match(template, /both hashed Windows locks/);
  assert.match(template, /Signed-off-by/);
});

test("release workflow gates tags and publishes checksummed attested archives", async () => {
  const workflow = await fs.readFile(".github/workflows/release.yml", "utf8");
  const parsed = parseYaml(workflow.replaceAll("\n", "\r\n"));
  assert.equal(parsed.permissions.contents, "write");
  assert.equal(parsed.permissions.attestations, "write");
  assert.match(workflow, /tags:\n\s+- "v\*\.\*\.\*"/);
  assert.match(workflow, /verify-release-ancestry\.mjs/);
  assert.equal((workflow.match(/verify-release-tag\.mjs/g) || []).length, 2);
  assert.match(workflow, /attest-build-provenance@[0-9a-f]{40,64}/);
  assert.match(workflow, /Expected two ZIPs and two checksum files/);
  assert.match(workflow, /gh release create/);
  assert.match(workflow, /--verify-tag/);
  assert.match(workflow, /python -m pip_audit --local/);
  assert.doesNotMatch(workflow, /pip_audit --local[^\n]*--ignore-vuln/);
  assert.match(workflow, /name: Hashed Windows service runtime \/ Python/);
  assert.match(workflow, /python-version: \["3\.11", "3\.14"\]/);
  assert.match(workflow, /needs: \[service-locks\]/);
  assert.match(workflow, /--require-hashes --only-binary=:all: --report \$report/);
  assert.match(workflow, /verify_service_lock_install\.py/);
  assert.match(workflow, /scripts\/verify-lifecycle-provisioning\.py/);
  assert.match(workflow, /pip-audit==2\.10\.1/);
  assert.equal(
    (workflow.match(/pip_audit --requirement install\/locks\/windows-cpython-31[14]-x86_64\.txt --no-deps/g) || []).length,
    2,
  );

  const uses = [...workflow.matchAll(/^\s*-?\s*uses:\s+([^\s#]+).*$/gm)].map(
    (match) => match[1],
  );
  for (const action of uses) assert.match(action, /^[^@\s]+@[0-9a-f]{40,64}$/);
});

test("archive hygiene rejects private state and generated output", () => {
  assert.throws(
    () =>
      assertArchiveHygiene([
        "plugin.js",
        "jobs/client/page_1.html",
        ".env",
        ".test-tmp/private/run.json",
      ]),
    /Forbidden release paths/,
  );
  assert.throws(() => assertArchiveHygiene(["dist/release.zip"]), /Forbidden release paths/);
  assert.throws(
    () =>
      assertArchiveHygiene([
        "hermes-document-reader-source-v0.1.0/dist/release.zip",
      ]),
    /Forbidden release paths/,
  );
  assert.doesNotThrow(() =>
    assertArchiveHygiene([
      "plugin.js",
      "plugin.yaml",
      "LICENSE",
      "dashboard/dist/index.js",
    ]),
  );
});

test("release ancestry dereferences the tag commit and accepts a main ancestor", () => {
  const calls = [];
  const runGit = (args) => {
    calls.push(args);
    if (args[0] === "rev-parse") return "abc123";
    if (args[0] === "merge-base") return "";
    throw new Error(`unexpected git command: ${args.join(" ")}`);
  };
  assert.equal(verifyReleaseAncestry("v0.1.0", "origin/main", runGit), "abc123");
  assert.deepEqual(calls, [
    ["rev-parse", "v0.1.0^{commit}"],
    ["merge-base", "--is-ancestor", "abc123", "origin/main"],
  ]);
});

test("release ancestry rejects a tag outside main", () => {
  const runGit = (args) => {
    if (args[0] === "rev-parse") return "def456";
    throw new Error("not an ancestor");
  };
  assert.throws(
    () => verifyReleaseAncestry("v0.1.0", "origin/main", runGit),
    /not reachable from origin\/main/,
  );
});

test("release tag identity matches annotated local, event, and remote refs", () => {
  const runGit = (args) => {
    const command = args.join(" ");
    if (command === "cat-file -t refs/tags/v0.1.0") return "tag";
    if (command === "rev-parse refs/tags/v0.1.0") return "tag-object";
    if (command === "rev-parse refs/tags/v0.1.0^{commit}") return "release-commit";
    if (command === "rev-parse event-sha^{commit}") return "release-commit";
    if (command.startsWith("ls-remote --tags origin")) {
      return [
        "tag-object\trefs/tags/v0.1.0",
        "release-commit\trefs/tags/v0.1.0^{}",
      ].join("\n");
    }
    throw new Error(`unexpected git command: ${command}`);
  };
  assert.deepEqual(verifyReleaseTag("v0.1.0", "event-sha", runGit), {
    commit: "release-commit",
    tagObject: "tag-object",
  });
});

test("release tag identity rejects a moved remote tag", () => {
  const runGit = (args) => {
    const command = args.join(" ");
    if (command.startsWith("cat-file")) return "tag";
    if (command === "rev-parse refs/tags/v0.1.0") return "tag-object";
    if (command.startsWith("rev-parse")) return "release-commit";
    if (command.startsWith("ls-remote")) {
      return [
        "other-tag-object\trefs/tags/v0.1.0",
        "other-commit\trefs/tags/v0.1.0^{}",
      ].join("\n");
    }
    throw new Error(`unexpected git command: ${command}`);
  };
  assert.throws(
    () => verifyReleaseTag("v0.1.0", "event-sha", runGit),
    /remote tag object changed/,
  );
});
