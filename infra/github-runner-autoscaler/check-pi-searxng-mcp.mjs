#!/usr/bin/env node
import { spawn } from 'node:child_process';
import fs from 'node:fs';

const CONFIG_PATH = process.env.PI_MCP_ADAPTER_CONFIG ?? '/home/runner/.pi/agent/mcp-adapter.json';
const SEARCH_TOOL = 'searxng_web_search';
const QUERY = 'SearXNG';
const TIMEOUT_MS = 20_000;

function fail(message) {
  throw new Error(`Pi SearXNG MCP preflight failed: ${message}`);
}

export async function checkSearxngMcp({ configPath = CONFIG_PATH, spawnProcess = spawn } = {}) {
  let config;
  try {
    config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  } catch (error) {
    if (error.code === 'ENOENT') fail(`adapter config is missing (${configPath})`);
    fail(`adapter config is unreadable or invalid JSON (${configPath})`);
  }

  const server = config.mcpServers?.searxng;
  if (!server || typeof server.command !== 'string' || !server.command.trim()) {
    fail('mcp-adapter.json must define mcpServers.searxng with a command');
  }
  if (server.command !== 'mcp-searxng') {
    fail('the configured SearXNG command must be the image-pinned mcp-searxng executable');
  }
  if (typeof server.env?.SEARXNG_URL !== 'string' || !server.env.SEARXNG_URL.trim()) {
    fail('mcpServers.searxng must define SEARXNG_URL');
  }

  let child;
  try {
    child = spawnProcess(server.command, Array.isArray(server.args) ? server.args : [], {
      cwd: process.cwd(),
      env: { ...process.env, ...(server.env ?? {}) },
      stdio: ['pipe', 'pipe', 'ignore'],
    });
  } catch {
    fail('mcp-searxng could not be started; check image PATH and package installation');
  }
  let buffer = '';
  let nextId = 1;
  const pending = new Map();
  let settled = false;

  const cleanup = () => {
    child.stdout.off('data', onData);
    child.off('error', onError);
    child.off('exit', onExit);
    clearTimeout(timer);
    for (const { reject } of pending.values()) reject(new Error('closed'));
    pending.clear();
  };
  const finish = (error) => {
    if (settled) return;
    settled = true;
    if (error) {
      for (const { reject } of pending.values()) reject(error);
      pending.clear();
    }
    cleanup();
    child.kill('SIGTERM');
  };
  const send = (message) => child.stdin.write(`${JSON.stringify(message)}\n`);
  const request = (method, params = {}) => new Promise((resolve, reject) => {
    const id = nextId++;
    pending.set(id, { resolve, reject });
    send({ jsonrpc: '2.0', id, method, params });
  });
  const onData = (chunk) => {
    buffer += chunk.toString('utf8');
    while (true) {
      const newline = buffer.indexOf('\n');
      if (newline < 0) return;
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (!line) continue;
      let message;
      try {
        message = JSON.parse(line);
      } catch {
        finish(new Error('Pi SearXNG MCP preflight failed: server emitted invalid stdio JSON-RPC'));
        return;
      }
      const response = pending.get(message.id);
      if (!response) continue;
      pending.delete(message.id);
      if (message.error) response.reject(new Error('MCP request failed'));
      else response.resolve(message.result);
    }
  };
  const onError = () => finish(new Error('Pi SearXNG MCP preflight failed: mcp-searxng could not be started (check image PATH and package installation)'));
  const onExit = (code) => {
    if (!settled) finish(new Error(`Pi SearXNG MCP preflight failed: mcp-searxng exited before completing the handshake (status ${code ?? 'unknown'})`));
  };
  const timer = setTimeout(() => finish(new Error('Pi SearXNG MCP preflight failed: handshake/search timed out; check the SearXNG backend URL and network reachability')), TIMEOUT_MS);

  child.stdout.on('data', onData);
  child.on('error', onError);
  child.on('exit', onExit);

  try {
    const initialized = await request('initialize', {
      protocolVersion: '2025-03-26',
      capabilities: {},
      clientInfo: { name: 'social-mcp-runner-preflight', version: '1.0.0' },
    });
    if (!initialized?.protocolVersion || !initialized?.serverInfo?.name) {
      fail('server returned an invalid MCP initialize response');
    }
    send({ jsonrpc: '2.0', method: 'notifications/initialized' });

    const listed = await request('tools/list');
    if (!Array.isArray(listed?.tools) || !listed.tools.some((tool) => tool.name === SEARCH_TOOL)) {
      fail(`server did not advertise ${SEARCH_TOOL}`);
    }
    const result = await request('tools/call', { name: SEARCH_TOOL, arguments: { query: QUERY } });
    if (!result || result.isError || !Array.isArray(result.content) || result.content.length === 0) {
      fail('read-only search call returned no successful content; check SearXNG JSON search and connectivity');
    }
    finish();
    return { server: 'searxng', tool: SEARCH_TOOL, handshake: true, search: true };
  } catch (error) {
    if (!settled) finish(error instanceof Error && error.message.startsWith('Pi SearXNG MCP preflight failed:')
      ? error
      : new Error('Pi SearXNG MCP preflight failed: initialize or read-only search request was rejected; check backend availability and JSON search configuration'));
    throw error;
  }
}

if (process.argv[1] && new URL(import.meta.url).pathname === process.argv[1]) {
  checkSearxngMcp().then(() => {
    process.stdout.write('Pi SearXNG MCP preflight passed: initialize and read-only search succeeded\n');
  }).catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}
