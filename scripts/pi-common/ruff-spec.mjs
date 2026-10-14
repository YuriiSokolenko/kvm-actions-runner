import fs from 'node:fs';
import path from 'node:path';

/** The repository-owned Ruff contract used by focused and final checks. */
export function ruffArgs(root, targets, { fix = false, json = false } = {}) {
  const config = path.join(fs.realpathSync(root), 'pyproject.toml');
  return ['check', ...(fix ? ['--fix'] : []), ...(json ? ['--output-format=json'] : []), '--config', config, ...targets];
}
