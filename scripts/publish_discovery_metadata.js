/**
 * publish_discovery_metadata.js — ship discovery metadata to the public site.
 *
 * Every minted NFT stores an immutable `uri` of
 * <SITE_ORIGIN>/discoveries/<n>.json. That URL must resolve by display time or
 * wallets and explorers show a blank token, and the mint cannot be corrected.
 * This copies output/discovery_metadata/*.json into the site repo's
 * discoveries/ directory and pushes, making those URLs live.
 *
 * Run after any mint. Safe to re-run: unchanged files are skipped and nothing
 * is committed when there is no diff.
 *
 * Usage:
 *   node scripts/publish_discovery_metadata.js
 *   node scripts/publish_discovery_metadata.js --dry-run
 *   node scripts/publish_discovery_metadata.js --src /path/to/discovery_metadata
 *   SITE_REPO=/path/to/life-compute.github.io node scripts/publish_discovery_metadata.js
 */

'use strict';

const { execFileSync } = require('child_process');
const fs   = require('fs');
const path = require('path');

const REPO      = path.resolve(__dirname, '..');
const argv      = process.argv.slice(2);
const srcFlag   = argv.indexOf('--src');
// The mint writes metadata beside its registry, which is not always
// REPO/output — it passes --src so we ship the files it actually wrote.
const SRC_DIR   = srcFlag !== -1 ? path.resolve(argv[srcFlag + 1])
                                 : path.join(REPO, 'output/discovery_metadata');
const SITE_REPO = process.env.SITE_REPO || '/tmp/life-compute.github.io';
const DEST_DIR  = path.join(SITE_REPO, 'discoveries');
const dryRun    = argv.includes('--dry-run');

const git = (args, cwd = SITE_REPO) =>
  execFileSync('git', args, { cwd, encoding: 'utf8' }).trim();

function main() {
  if (!fs.existsSync(SRC_DIR)) {
    console.log(`No metadata to publish (${SRC_DIR} does not exist)`);
    return 0;
  }
  if (!fs.existsSync(path.join(SITE_REPO, '.git'))) {
    console.error(`BLOCKER: site repo not found at ${SITE_REPO}. ` +
                  `Clone it or set SITE_REPO.`);
    return 1;
  }

  const files = fs.readdirSync(SRC_DIR).filter((f) => f.endsWith('.json'));
  if (!files.length) {
    console.log('No metadata files to publish');
    return 0;
  }

  fs.mkdirSync(DEST_DIR, { recursive: true });

  const changed = [];
  for (const f of files) {
    const src = fs.readFileSync(path.join(SRC_DIR, f), 'utf8');
    const dst = path.join(DEST_DIR, f);
    if (fs.existsSync(dst) && fs.readFileSync(dst, 'utf8') === src) continue;
    if (!dryRun) fs.writeFileSync(dst, src);
    changed.push(f);
  }

  console.log(`${files.length} metadata file(s), ${changed.length} new/changed` +
              (changed.length ? `: ${changed.join(', ')}` : ''));

  if (!changed.length) {
    console.log('Nothing to publish — site already up to date');
    return 0;
  }
  if (dryRun) {
    console.log('DRY RUN — no files written, nothing pushed');
    return 0;
  }

  git(['add', 'discoveries']);
  if (!git(['diff', '--cached', '--name-only'])) {
    console.log('Nothing staged — site already up to date');
    return 0;
  }
  git(['commit', '-m',
       `chore(discoveries): publish metadata for ${changed.length} discovery(s)`]);
  git(['push', 'origin', 'HEAD']);
  console.log(`Published ${changed.length} file(s) → ${SITE_REPO}/discoveries/`);
  console.log('Note: GitHub Pages takes ~30-60s to rebuild before URLs resolve.');
  return 0;
}

process.exit(main());
