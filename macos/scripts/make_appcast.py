"""Writes the Sparkle update feed for one chip's disk image: the feed a release build of Workstreams.app checks.

    python3 macos/scripts/make_appcast.py --version 0.5.0 --chip intel --dmg-url URL --signature SIG \\
        --length N --release-url URL > appcast-intel.xml

The signature and length are what Sparkle's sign_update prints for the disk image. CI runs this for each chip on a
tag and attaches the feeds to the release (.github/workflows/ci.yml); the apps find them through
releases/latest/download/appcast-CHIP.xml. One item per feed: Sparkle only needs the newest release.
"""

import argparse
import xml.etree.ElementTree as ET

SPARKLE = "http://www.andymatuschak.org/xml-namespaces/sparkle"
MINIMUM_SYSTEM_VERSION = "14.0"  # LSMinimumSystemVersion in macos/scripts/build-app.sh


def render(version: str, chip: str, dmg_url: str, signature: str, length: int, release_url: str) -> str:
    ET.register_namespace("sparkle", SPARKLE)
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = f"Workstreams ({chip})"
    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = f"Workstreams {version}"
    ET.SubElement(item, f"{{{SPARKLE}}}version").text = version
    ET.SubElement(item, f"{{{SPARKLE}}}shortVersionString").text = version
    ET.SubElement(item, f"{{{SPARKLE}}}minimumSystemVersion").text = MINIMUM_SYSTEM_VERSION
    ET.SubElement(item, f"{{{SPARKLE}}}releaseNotesLink").text = release_url
    ET.SubElement(item, "enclosure", {
        "url": dmg_url,
        "length": str(length),
        "type": "application/octet-stream",
        f"{{{SPARKLE}}}edSignature": signature,
    })
    ET.indent(rss)
    return ET.tostring(rss, encoding="unicode", xml_declaration=True) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Write a Sparkle update feed for one chip's disk image.")
    parser.add_argument("--version", required=True)
    parser.add_argument("--chip", required=True, choices=["apple-silicon", "intel"])
    parser.add_argument("--dmg-url", required=True)
    parser.add_argument("--signature", required=True, help="sparkle:edSignature, from sign_update")
    parser.add_argument("--length", required=True, type=int, help="the disk image's size in bytes")
    parser.add_argument("--release-url", required=True, help="the release page, shown as the release notes")
    args = parser.parse_args()
    print(render(args.version, args.chip, args.dmg_url, args.signature, args.length, args.release_url), end="")


if __name__ == "__main__":
    main()
