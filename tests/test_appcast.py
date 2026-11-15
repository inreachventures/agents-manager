"""The Sparkle update feed CI publishes with each release: macos/scripts/make_appcast.py."""

import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "macos" / "scripts" / "make_appcast.py"
SPARKLE = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"


def load():
    spec = importlib.util.spec_from_file_location("make_appcast", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_render():
    xml = load().render(
        "0.5.0",
        "intel",
        "https://github.com/inreachventures/agents-manager/releases/download/v0.5.0/Workstreams-0.5.0-intel.dmg",
        "abc==",
        123,
        "https://github.com/inreachventures/agents-manager/releases/tag/v0.5.0",
    )
    root = ET.fromstring(xml)
    [item] = root.iter("item")
    assert item.findtext(f"{SPARKLE}version") == "0.5.0"
    assert item.findtext(f"{SPARKLE}shortVersionString") == "0.5.0"
    assert item.findtext(f"{SPARKLE}minimumSystemVersion") == "14.0"
    assert item.findtext(f"{SPARKLE}releaseNotesLink") == (
        "https://github.com/inreachventures/agents-manager/releases/tag/v0.5.0"
    )
    enclosure = item.find("enclosure")
    assert enclosure.get("url").endswith("-intel.dmg")
    assert enclosure.get("length") == "123"
    assert enclosure.get(f"{SPARKLE}edSignature") == "abc=="
    assert enclosure.get("type") == "application/octet-stream"


def test_render_escapes_urls():
    xml = load().render("0.5.0", "intel", "https://example.com/a.dmg?x=1&y=2", "abc==", 1, "https://example.com/r")
    [item] = ET.fromstring(xml).iter("item")
    assert item.find("enclosure").get("url") == "https://example.com/a.dmg?x=1&y=2"
