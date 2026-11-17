import importlib.resources
import json
import shutil
import subprocess

import pytest

PRLENS = importlib.resources.files("agentmgr") / "data" / "prlens"


def test_vendored_files_present():
    versions = json.loads((PRLENS / "versions.json").read_text())
    assert versions["renderer"] == "0.6.3"
    assert versions["schema"] == "0.10.0"
    assert versions["schemaVersion"] == "0.3.1"
    assert (PRLENS / "manual.md").is_file()
    assert (PRLENS / "prlens-validate.mjs").is_file()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_validator_accepts_example_and_rejects_dangling_edge(tmp_path):
    validator = str(PRLENS / "prlens-validate.mjs")
    example = PRLENS / "references" / "example.graph.json"

    def run(path):
        return subprocess.run(["node", validator, str(path)], capture_output=True, text=True)

    good = run(example)
    assert (good.returncode, good.stdout.strip()) == (0, "ok")

    doc = json.loads(example.read_text())
    doc["edges"][0]["to"] = "no-such-node"
    broken = tmp_path / "broken.graph.json"
    broken.write_text(json.dumps(doc))
    bad = run(broken)
    assert bad.returncode == 1
    assert "no-such-node" in bad.stdout
