import fs from 'node:fs';
import path from 'node:path';

const IGNORED_SCAN_DIRECTORIES = new Set(['.git', '.venv', 'venv', 'node_modules', '__pycache__']);

function containsPythonSource(directory) {
  const stack = [directory];
  while (stack.length) {
    const current = stack.pop();
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      if (entry.isSymbolicLink()) continue;
      const target = path.join(current, entry.name);
      if (entry.isFile() && /\.pyi?$/.test(entry.name)) return true;
      if (entry.isDirectory() && !IGNORED_SCAN_DIRECTORIES.has(entry.name)) stack.push(target);
    }
  }
  return false;
}

const relativeDirectory = (root, target) => `${path.relative(root, target).split(path.sep).join('/')}/`;

export function duplicatePackageRootDiagnostics(root, policy = {}) {
  const worktree = fs.realpathSync(path.resolve(root));
  const canonicalRoots = Array.isArray(policy.canonicalRoots) ? policy.canonicalRoots : [];
  const allowed = new Set(Array.isArray(policy.allowDuplicatePackages) ? policy.allowDuplicatePackages : []);
  const diagnostics = [];
  const seen = new Set();

  for (const configuredRoot of canonicalRoots) {
    const canonicalRoot = path.resolve(worktree, configuredRoot);
    const relativeRoot = path.relative(worktree, canonicalRoot);
    if (
      !relativeRoot
      || relativeRoot === '..'
      || relativeRoot.startsWith(`..${path.sep}`)
      || path.isAbsolute(relativeRoot)
      || !fs.existsSync(canonicalRoot)
      || !fs.lstatSync(canonicalRoot).isDirectory()
    ) continue;

    for (const entry of fs.readdirSync(canonicalRoot, { withFileTypes: true })) {
      if (!entry.isDirectory() || entry.isSymbolicLink() || allowed.has(entry.name)) continue;
      const canonicalPackage = path.join(canonicalRoot, entry.name);
      if (!containsPythonSource(canonicalPackage)) continue;

      const accidentalPackage = path.join(worktree, entry.name);
      if (
        !fs.existsSync(accidentalPackage)
        || !fs.lstatSync(accidentalPackage).isDirectory()
        || !containsPythonSource(accidentalPackage)
      ) continue;

      const canonicalPath = relativeDirectory(worktree, canonicalPackage);
      const accidentalPath = relativeDirectory(worktree, accidentalPackage);
      const key = `${accidentalPath}\0${canonicalPath}`;
      if (seen.has(key)) continue;
      seen.add(key);

      diagnostics.push({
        file: accidentalPath,
        line: null,
        column: null,
        code: 'DuplicatePackageRoot',
        message: `Top-level package ${accidentalPath} duplicates canonical package ${canonicalPath}. Remove ${accidentalPath} or add "${entry.name}" to checks.packageRoots.allowDuplicatePackages when the duplicate root is intentional.`,
      });
    }
  }

  return diagnostics;
}

export function assertNoDuplicatePackageRoots(root, policy = {}) {
  const diagnostics = duplicatePackageRootDiagnostics(root, policy);
  if (!diagnostics.length) return;
  throw new Error(`check: package roots\n${diagnostics.map(item => `${item.code}: ${item.message}`).join('\n')}`);
}
