import { existsSync, statSync } from 'node:fs';

// Shared by Implementer tool registration and the pinned pi-subagents adapter.
// Keep the function body self-contained: restoredWork.toString() is injected
// with existsSync and statSync supplied as globals in the patched adapter.
export function restoredWork(env = process.env) {
  if (env.PI_RESUME_ACTIVE != null) return env.PI_RESUME_ACTIVE === 'true';
  const patch = env.PI_RESUME_PATCH;
  try {
    if (!patch || !existsSync(patch)) return false;
    const file = statSync(patch);
    return file.isFile() && file.size > 0;
  } catch {
    return false;
  }
}
