#!/usr/bin/env node
import fs from 'node:fs';

const [metadataPath] = process.argv.slice(2);
if (!metadataPath) throw new Error('Usage: patch-pi-mcp-adapter.mjs <package.json>');

const metadata = JSON.parse(fs.readFileSync(metadataPath, 'utf8'));
if (metadata.name !== 'pi-mcp-adapter' || metadata.version !== '5.2.0') {
  throw new Error(`Expected pi-mcp-adapter@5.2.0; found ${metadata.name}@${metadata.version}`);
}

const peer = metadata.peerDependencies?.['@earendil-works/pi-ai'];
const upstreamPeer = '^0.84.1 || ^0.85.0 || ^0.86.0 || ^0.87.0 || ^0.99.0 || ^1.0.0';
if (peer === '^0.84.1 || ^0.85.0 || ^0.86.0 || ^0.87.0 || ^0.99.0 || ^1.0.0 || ^1.1.0') {
  process.exit(0);
}
if (peer !== upstreamPeer) throw new Error(`Unexpected pi-ai peer range: ${peer}`);

metadata.peerDependencies['@earendil-works/pi-ai'] = `${upstreamPeer} || ^1.1.0`;
fs.writeFileSync(metadataPath, `${JSON.stringify(metadata, null, 2)}\n`);
