"""The map page's pure functions (macos/Resources/map/map.js), run under node against PR Lens's renderer bundle."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAP_JS = ROOT / "macos" / "Resources" / "map" / "map.js"
PRLENS_JS = ROOT / "macos" / "Resources" / "map" / "prlens.js"
EXAMPLE = ROOT / "src" / "agentmgr" / "data" / "prlens" / "references" / "example.graph.json"

# Splits the example's lanes between two repos, narrows the document to each, and checks every narrowed document
# still parses and renders in both lenses. Prints one JSON line per repo.
SCRIPT = r"""
const fs = require("fs"), vm = require("vm");
const [mapJs, prlensJs, examplePath] = process.argv.slice(1);
vm.runInThisContext(fs.readFileSync(prlensJs, "utf8"));
const page = require(mapJs);
const doc = JSON.parse(fs.readFileSync(examplePath, "utf8"));
const repoOf = { web: "front", functions: "back", external: "back" };
const lane = id => `${repoOf[id]}/${id}`;
doc.lanes.forEach(l => { l.id = lane(l.id); });
doc.nodes.forEach(n => { n.lane = lane(n.lane); });
delete doc.views; delete doc.walkthrough; delete doc.layout;  // they name lanes by their old ids
for (const repo of ["front", "back"]) {
  const out = page.filterToRepo(doc, repo);
  const ids = new Set(out.nodes.map(n => n.id));
  const parsed = PrLens.parse(JSON.stringify(out));
  const rendered = page.lensesOf(out).filter(l => l !== "data-model").map(lens => {
    const { svg } = PrLens.render(parsed.doc, { lens, theme: "light" });
    return svg.startsWith("<svg") ? lens : null;
  });
  console.log(JSON.stringify({
    repo, parsed: parsed.ok, issues: parsed.issues || [], rendered,
    lanes: out.lanes.map(l => l.id),
    own: out.nodes.filter(n => n.lane.startsWith(repo + "/")).length,
    context: out.nodes.filter(n => n.lane === "context").length,
    dangling: out.edges.filter(e => !ids.has(e.from) || !ids.has(e.to)).length +
      out.flows.flatMap(f => f.messages).filter(m => !ids.has(m.from) || !ids.has(m.to)).length,
    untouched: doc.lanes[0].id === lane("web"),
  }));
}
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_filter_to_repo_keeps_a_drawable_document():
    proc = subprocess.run(["node", "-e", SCRIPT, str(MAP_JS), str(PRLENS_JS), str(EXAMPLE)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    results = {r["repo"]: r for r in map(json.loads, proc.stdout.splitlines())}
    example = json.loads(EXAMPLE.read_text())
    web_nodes = sum(n["lane"] == "web" for n in example["nodes"])
    for repo, r in results.items():
        assert r["parsed"], r["issues"]
        assert r["rendered"] == ["architecture", "data-flow"]
        assert r["dangling"] == 0
        assert r["untouched"]  # the document handed in is left alone
        assert all(lane.startswith(f"{repo}/") or lane == "context" for lane in r["lanes"])
    assert results["front"]["own"] == web_nodes
    assert results["back"]["own"] == len(example["nodes"]) - web_nodes
    # the cross-repo calls in the example keep the other side visible in the "Other repos" lane
    assert results["front"]["context"] > 0 and "context" in results["front"]["lanes"]
