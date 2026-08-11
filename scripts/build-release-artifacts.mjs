#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

import { verifyReleaseMetadata } from "./verify-release-metadata.mjs";

const SEMVER = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/;
const RELEASE_CONTRACT_FILES = [
  "CHANGELOG.md",
  "dashboard/manifest.json",
  "desktop-plugin/document-reader/plugin.js",
  "install/locks/windows-cpython-311-x86_64.txt",
  "install/locks/windows-cpython-314-x86_64.txt",
  "package-lock.json",
  "package.json",
  "plugin.yaml",
  "profile_runtime.py",
  "scripts/lock-inputs/service-bootstrap.txt",
  "scripts/plugin-release-files.json",
  "scripts/regenerate-service-locks.ps1",
  "service/ocr_service.py",
];

function argument(name) {
  const index = process.argv.indexOf(name);
  return index === -1 ? "" : process.argv[index + 1] || "";
}

function git(root, args, options = {}) {
  return execFileSync("git", args, {
    cwd: root,
    encoding: options.encoding || "utf8",
    maxBuffer: 32 * 1024 * 1024,
    stdio: options.stdio || ["ignore", "pipe", "pipe"],
  });
}

function checksum(file) {
  return createHash("sha256").update(fs.readFileSync(file)).digest("hex");
}

function writeChecksum(file) {
  const digest = checksum(file);
  fs.writeFileSync(`${file}.sha256`, `${digest}  ${path.basename(file)}\n`, "ascii");
}

function requireContractInTree(root, treeish) {
  for (const relativePath of RELEASE_CONTRACT_FILES) {
    let committed;
    try {
      committed = git(root, ["show", `${treeish}:${relativePath}`]);
    } catch {
      throw new Error(
        `${relativePath} is not present in ${treeish}; commit the complete release contract before building`,
      );
    }
    const working = fs.readFileSync(path.join(root, relativePath), "utf8");
    if (committed !== working) {
      throw new Error(
        `${relativePath} differs from ${treeish}; release archives must come from the exact reviewed Git tree`,
      );
    }
  }
}

function requireExactCommittedTree(root, treeish) {
  const headCommit = git(root, ["rev-parse", "HEAD^{commit}"]).trim();
  const requestedCommit = git(root, ["rev-parse", `${treeish}^{commit}`]).trim();
  if (requestedCommit !== headCommit) {
    throw new Error(
      `Release tree ${treeish} resolves to ${requestedCommit}, not checked-out HEAD ${headCommit}`,
    );
  }
  const status = git(root, ["status", "--porcelain=v1", "--untracked-files=all"]).trim();
  if (status) {
    throw new Error(
      `Release archives require a clean committed tree; working tree changes:\n${status}`,
    );
  }
}

function pluginReleaseFiles(root) {
  const manifestPath = path.join(root, "scripts", "plugin-release-files.json");
  const parsed = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  const files = parsed.files;
  if (!Array.isArray(files) || files.length === 0) {
    throw new Error("scripts/plugin-release-files.json must contain a non-empty files array");
  }
  const normalized = files.map((file) => String(file).replaceAll("\\", "/"));
  if (new Set(normalized).size !== normalized.length) {
    throw new Error("scripts/plugin-release-files.json contains duplicate paths");
  }
  for (const file of normalized) {
    if (
      !file ||
      path.posix.isAbsolute(file) ||
      file.split("/").includes("..") ||
      file.endsWith("/")
    ) {
      throw new Error(`Unsafe plugin release path: ${file}`);
    }
  }
  const sorted = [...normalized].sort();
  if (JSON.stringify(normalized) !== JSON.stringify(sorted)) {
    throw new Error("scripts/plugin-release-files.json paths must be sorted");
  }
  return normalized;
}

export function buildReleaseArtifacts({
  root = process.cwd(),
  outputDir = "dist",
  treeish = "HEAD",
  version = "",
} = {}) {
  const resolvedRoot = path.resolve(root);
  const metadataVersion = verifyReleaseMetadata({ root: resolvedRoot }).version;
  const releaseVersion = version || metadataVersion;
  if (!SEMVER.test(releaseVersion)) throw new Error(`Invalid release version: ${releaseVersion}`);
  if (releaseVersion !== metadataVersion) {
    throw new Error(
      `Requested release version ${releaseVersion} does not match metadata version ${metadataVersion}`,
    );
  }

  git(resolvedRoot, ["rev-parse", `${treeish}^{tree}`]);
  requireExactCommittedTree(resolvedRoot, treeish);
  requireContractInTree(resolvedRoot, treeish);
  const pluginFiles = pluginReleaseFiles(resolvedRoot);
  for (const file of pluginFiles) {
    try {
      git(resolvedRoot, ["cat-file", "-e", `${treeish}:${file}`]);
    } catch {
      throw new Error(
        `Required installable plugin file ${file} is missing from ${treeish}`,
      );
    }
  }

  const resolvedOutput = path.resolve(resolvedRoot, outputDir);
  fs.mkdirSync(resolvedOutput, { recursive: true });

  const productName = `hermes-document-reader-source-v${releaseVersion}.zip`;
  const pluginName = `hermes-document-reader-v${releaseVersion}.zip`;
  const productPath = path.join(resolvedOutput, productName);
  const pluginPath = path.join(resolvedOutput, pluginName);
  for (const file of [productPath, pluginPath, `${productPath}.sha256`, `${pluginPath}.sha256`]) {
    fs.rmSync(file, { force: true });
  }

  git(
    resolvedRoot,
    [
      "archive",
      "--format=zip",
      `--prefix=hermes-document-reader-source-v${releaseVersion}/`,
      `--output=${productPath}`,
      treeish,
    ],
    { stdio: "inherit" },
  );

  git(
    resolvedRoot,
    [
      "archive",
      "--format=zip",
      `--output=${pluginPath}`,
      treeish,
      "--",
      ...pluginFiles,
    ],
    { stdio: "inherit" },
  );

  writeChecksum(productPath);
  writeChecksum(pluginPath);
  return { productPath, pluginPath, version: releaseVersion, treeish };
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) {
  try {
    const result = buildReleaseArtifacts({
      outputDir: argument("--output-dir") || "dist",
      treeish: argument("--tree") || process.env.RELEASE_TREEISH || "HEAD",
      version: argument("--version"),
    });
    process.stdout.write(
      `Built ${path.basename(result.productPath)} and ${path.basename(result.pluginPath)} from ${result.treeish}\n`,
    );
  } catch (error) {
    console.error(error instanceof Error ? error.message : "Release artifact build failed");
    process.exit(1);
  }
}
