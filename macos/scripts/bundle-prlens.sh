#!/bin/bash
# Vendors PR Lens (https://github.com/coldteadotai/pr-lens, MIT) so Workstreams.app can draw a task map without Node at
# runtime, and so a background Claude process can validate a draft without downloading anything from npm.
#
# Needs Node 20+, npm and an authenticated gh. Installs the pinned packages into a temp dir, bundles them with esbuild
# and writes these committed files:
#   macos/Resources/map/prlens.js                  browser IIFE: globalThis.PrLens = { render, parse }
#   src/agentmgr/data/prlens/prlens-validate.mjs   node CLI: prlens-validate.mjs <graph.json>
#   src/agentmgr/data/prlens/{manual.md,references/,versions.json}
# The packages are pinned below; bump them here, rerun, and commit the result. The bundles are left unminified on
# purpose, so an upgrade's diff can be reviewed.
set -euo pipefail

RENDERER=0.6.3
SCHEMA=0.10.0
ESBUILD=0.28.2
# The agent-skill npm package ships only a stub, so manual.md and references/ come from the repository, at the commit
# whose packages/renderer and packages/schema are the versions above (v0.15.0 is older, so no tag fits).
SKILL_SOURCE=f15bc63ade4128381e84df4c3165dcba3307d6a2

root=$(cd "$(dirname "$0")/../.." && pwd)
page_out="$root/macos/Resources/map/prlens.js"
data_out="$root/src/agentmgr/data/prlens"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cd "$work"

npm init -y >/dev/null
npm install --no-save --silent --no-audit --no-fund \
  "@coldtea/pr-lens-renderer@$RENDERER" "@coldtea/pr-lens-schema@$SCHEMA" "esbuild@$ESBUILD"

fetch() { gh api "repos/coldteadotai/pr-lens/contents/$1?ref=$SKILL_SOURCE" -H 'Accept: application/vnd.github.raw'; }
mkdir -p skill/references
fetch LICENSE > skill/LICENSE
fetch packages/agent-skill/manual.md > skill/manual.md
for f in config.md graph-document.md example.graph.json; do fetch "packages/agent-skill/references/$f" > "skill/references/$f"; done

cat > page-entry.mjs <<'JS'
import { render } from "@coldtea/pr-lens-renderer";
import { safeParseGraphDoc } from "@coldtea/pr-lens-schema";

const parse = (json) => {
  let input;
  try {
    input = JSON.parse(json);
  } catch (error) {
    return { ok: false, issues: [{ path: "", message: `not valid JSON: ${error.message}` }] };
  }
  const parsed = safeParseGraphDoc(input);
  if (parsed.ok) return { ok: true, doc: parsed.value };
  return { ok: false, issues: parsed.error.issues.map(({ path, message }) => ({ path, message })) };
};

export { render, parse };
JS

cat > validate-entry.mjs <<'JS'
import { readFileSync } from "node:fs";
import { safeParseGraphDoc } from "@coldtea/pr-lens-schema";

const file = process.argv[2];
if (!file) {
  console.error("usage: prlens-validate.mjs <graph.json>");
  process.exit(2);
}
let input;
try {
  input = JSON.parse(readFileSync(file, "utf8"));
} catch (error) {
  console.log(`<root>: not valid JSON: ${error.message}`);
  process.exit(1);
}
// safeParseGraphDoc covers structure, contract version and referential integrity, as the pr-lens CLI's validate does
const parsed = safeParseGraphDoc(input);
if (parsed.ok) {
  console.log("ok");
} else {
  for (const { path, message } of parsed.error.issues) console.log(`${path || "<root>"}: ${message}`);
  process.exit(1);
}
JS

{
  echo '/*'
  sed 's/^/ * /; s/ *$//' skill/LICENSE
  echo ' *'
  echo " * PR Lens @coldtea/pr-lens-renderer $RENDERER and @coldtea/pr-lens-schema $SCHEMA, bundled with esbuild $ESBUILD."
  echo ' */'
} > banner.txt
esb=node_modules/.bin/esbuild
# the renderer's index re-exports manifest.js, which imports node:crypto for content hashes; the page never calls it, so
# give the page build a stub to keep Node out of the bundle
echo 'export const createHash = () => { throw new Error("node:crypto is not available in the page"); };' > crypto-stub.mjs
$esb page-entry.mjs --alias:node:crypto=./crypto-stub.mjs --bundle --format=iife --global-name=PrLens --platform=browser --target=es2020 \
  --legal-comments=none --log-level=warning --outfile=page.js
# the ESM bundle may carry CommonJS requires of node builtins; give them a require
$esb validate-entry.mjs --bundle --format=esm --platform=node --target=node20 --legal-comments=none \
  --log-level=warning --banner:js="import { createRequire as __cr } from 'node:module'; const require = __cr(import.meta.url);" \
  --outfile=validate.mjs
{ cat banner.txt page.js; } > page-final.js
{ cat banner.txt validate.mjs; } > validate-final.mjs

# smoke checks, before anything is written
example=skill/references/example.graph.json
cat > smoke.mjs <<'JS'
import { readFileSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import vm from "node:vm";

const [, , page, validator, example] = process.argv;
const fail = (message) => {
  console.error(`FAILED: ${message}`);
  process.exit(1);
};
// the page runs in a browser, so give the bare context the globals a browser has (zod validates URLs with URL)
const sandbox = { URL, TextEncoder, TextDecoder };
sandbox.globalThis = sandbox;
vm.runInNewContext(readFileSync(page, "utf8"), sandbox);
const { render, parse } = sandbox.PrLens ?? fail("page bundle did not set globalThis.PrLens");
const json = readFileSync(example, "utf8");

const parsed = parse(json);
if (!parsed.ok) fail(`parse rejected the example: ${JSON.stringify(parsed.issues)}`);
for (const lens of ["architecture", "data-flow"]) {
  for (const theme of ["light", "dark"]) {
    const out = render(parsed.doc, { lens, theme });
    if (!out.svg.startsWith("<svg")) fail(`render ${lens}/${theme} did not give an svg`);
  }
}
console.log("ok: render, both lenses and both themes, gives <svg");

// map.js finds the data-flow lens's cards by `g.cardsh > rect.card`, one per participant of each flow, in order
const flowSvg = render(parsed.doc, { lens: "data-flow", theme: "light" }).svg;
const participants = parsed.doc.flows.reduce((n, flow) => n + flow.participants.length, 0);
const cards = (flowSvg.match(/<g class="cardsh"[^>]*><rect class="card[ "]/g) || []).length;
if (participants === 0 || cards !== participants) fail(`data-flow lens: ${cards} g.cardsh > rect.card for ${participants} participants`);
console.log(`ok: the data-flow lens draws g.cardsh > rect.card for each of its ${participants} participants`);

const bad = parse("{}");
if (bad.ok || !bad.issues.length) fail("parse accepted an empty document");
console.log("ok: parse reports issues for an invalid document");

const run = (file) => spawnSync("node", [validator, file], { encoding: "utf8" });
let result = run(example);
if (result.status !== 0 || result.stdout.trim() !== "ok") fail(`validator on the example: ${result.status} ${result.stdout}`);
console.log("ok: validator accepts the example");

const doc = JSON.parse(json);
doc.edges[0].to = "no-such-node";
writeFileSync("broken.graph.json", JSON.stringify(doc));
result = run("broken.graph.json");
if (result.status !== 1 || !result.stdout.includes("no-such-node")) fail(`validator on a dangling edge: ${result.status} ${result.stdout}`);
console.log("ok: validator rejects a dangling edge");
JS
node smoke.mjs page-final.js "$PWD/validate-final.mjs" "$example"
if grep -q 'node:crypto' page-final.js; then echo "FAILED: page bundle contains node:crypto" >&2; exit 1; fi

mkdir -p "$(dirname "$page_out")" "$data_out/references"
cp page-final.js "$page_out"
cp validate-final.mjs "$data_out/prlens-validate.mjs"
cp skill/manual.md "$data_out/manual.md"
cp skill/references/* "$data_out/references/"
installed() { node -p "require('./node_modules/$1/package.json').version"; }
schema_version=$(node --input-type=module -e 'import { SCHEMA_VERSION } from "@coldtea/pr-lens-schema"; console.log(SCHEMA_VERSION)')
cat > "$data_out/versions.json" <<JSON
{
  "renderer": "$(installed @coldtea/pr-lens-renderer)",
  "schema": "$(installed @coldtea/pr-lens-schema)",
  "schemaVersion": "$schema_version",
  "skillSource": "$SKILL_SOURCE"
}
JSON

echo "wrote:"
for f in "$page_out" "$data_out/prlens-validate.mjs" "$data_out/manual.md" "$data_out/references"/* "$data_out/versions.json"; do
  echo "  ${f#"$root"/}"
done
