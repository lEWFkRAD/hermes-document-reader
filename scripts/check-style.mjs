#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";

import { parseDocument } from "yaml";

const ROOT = process.cwd();
const IGNORE_DIRS = new Set([
  ".git",
  ".pytest_cache",
  ".test-tmp",
  ".venv",
  "dist",
  "jobs",
  "node_modules",
  "venv",
]);
const TEXT_EXTENSIONS = new Set([
  ".css",
  ".html",
  ".js",
  ".json",
  ".md",
  ".mjs",
  ".ps1",
  ".py",
  ".txt",
  ".yml",
  ".yaml",
]);
const failures = [];

function relative(file) {
  return path.relative(ROOT, file).replaceAll("\\", "/");
}

async function walk(dir, files = []) {
  for (const entry of await fs.readdir(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      const ignored =
        IGNORE_DIRS.has(entry.name) ||
        entry.name === "__pycache__" ||
        entry.name.startsWith("pytest-cache-files-");
      if (!ignored) await walk(path.join(dir, entry.name), files);
    } else if (entry.isFile()) {
      files.push(path.join(dir, entry.name));
    }
  }
  return files;
}

function checkText(file, content) {
  if (!content.endsWith("\n")) failures.push(`${relative(file)}: missing final newline`);
  content.split(/\r?\n/).forEach((line, index) => {
    if (/[ \t]+$/.test(line)) failures.push(`${relative(file)}:${index + 1}: trailing whitespace`);
  });
}

function checkJavaScript(file) {
  const result = spawnSync(process.execPath, ["--check", file], {
    cwd: ROOT,
    encoding: "utf8",
  });
  if (result.status !== 0) {
    failures.push(
      `${relative(file)}: JavaScript syntax check failed\n${result.stderr || result.stdout}`,
    );
  }
}

async function main() {
  const files = await walk(ROOT);
  for (const file of files) {
    const extension = path.extname(file).toLowerCase();
    if (!TEXT_EXTENSIONS.has(extension)) continue;
    const content = await fs.readFile(file, "utf8");
    checkText(file, content);
    if (extension === ".json") {
      try {
        JSON.parse(content);
      } catch (error) {
        failures.push(`${relative(file)}: invalid JSON: ${error.message}`);
      }
    }
    if (extension === ".yml" || extension === ".yaml") {
      const document = parseDocument(content, { prettyErrors: true, uniqueKeys: true });
      for (const error of document.errors) {
        failures.push(`${relative(file)}: invalid YAML: ${error.message}`);
      }
    }
    if (extension === ".js" || extension === ".mjs") checkJavaScript(file);
  }
  if (failures.length) {
    console.error(failures.join("\n"));
    process.exitCode = 1;
  } else {
    console.log(`Style check passed for ${files.length} files.`);
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
