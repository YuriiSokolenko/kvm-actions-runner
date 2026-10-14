#!/usr/bin/env node
import { readdir, readFile } from 'node:fs/promises';
import { execFileSync, spawn } from 'node:child_process';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const globalModules = execFileSync('npm', ['root', '-g'], { encoding: 'utf8' }).trim();
const serverEntry = path.join(globalModules, 'lsp-mcp-server', 'dist', 'index.js');
const daemonMarker = 'org.gradle.launcher.daemon.bootstrap.GradleDaemon';

async function gradleDaemonPids() {
  const pids = new Set();
  let entries;
  try {
    entries = await readdir('/proc');
  } catch {
    return pids;
  }
  await Promise.all(entries.filter((pid) => /^\d+$/.test(pid)).map(async (pid) => {
    try {
      const command = await readFile(`/proc/${pid}/cmdline`, 'utf8');
      if (command.includes(daemonMarker)) pids.add(Number(pid));
    } catch {
      // Processes can exit between reading /proc and opening cmdline.
    }
  }));
  return pids;
}

async function stopNewGradleDaemons(baseline) {
  // Kotlin LSP uses Gradle Tooling API, which may leave an idle Gradle daemon
  // after the LSP process exits. The runner is single-job/ephemeral; stop only
  // daemon PIDs that appeared while this MCP server was alive.
  await delay(500);
  const current = await gradleDaemonPids();
  const startedHere = [...current].filter((pid) => !baseline.has(pid));
  for (const pid of startedHere) {
    try {
      process.kill(pid, 'SIGTERM');
    } catch (error) {
      if (error.code !== 'ESRCH') throw error;
    }
  }
  const deadline = Date.now() + 8000;
  while (Date.now() < deadline) {
    const remaining = await gradleDaemonPids();
    if (startedHere.every((pid) => !remaining.has(pid))) return startedHere.length;
    await delay(200);
  }
  for (const pid of startedHere) {
    try {
      if ((await gradleDaemonPids()).has(pid)) process.kill(pid, 'SIGKILL');
    } catch (error) {
      if (error.code !== 'ESRCH') throw error;
    }
  }
  return startedHere.length;
}

const baseline = await gradleDaemonPids();
const child = spawn(process.execPath, [serverEntry, ...process.argv.slice(2)], {
  cwd: process.cwd(),
  env: process.env,
  stdio: 'inherit',
});
let receivedSignal = false;
for (const signal of ['SIGTERM', 'SIGINT', 'SIGHUP']) {
  process.on(signal, () => {
    receivedSignal = true;
    if (child.exitCode === null && child.signalCode === null) child.kill(signal);
  });
}

child.once('error', (error) => {
  console.error(`[lsp-mcp-server-wrapper] failed to start: ${error.message}`);
  process.exitCode = 1;
});
child.once('exit', async (code, signal) => {
  try {
    await stopNewGradleDaemons(baseline);
  } catch (error) {
    console.error(`[lsp-mcp-server-wrapper] Gradle cleanup failed: ${error.message}`);
    process.exitCode = 1;
    return;
  }
  if (receivedSignal) process.exitCode = 0;
  else process.exitCode = code ?? (signal ? 1 : 0);
});
