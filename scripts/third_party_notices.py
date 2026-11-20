"""Write THIRD_PARTY_NOTICES.txt for the Windows release bundle.

The PlaudTools zip ships three kinds of third-party code: the Python runtime,
every runtime dependency of plaud-tools[tray] (frozen in by PyInstaller), and a
GPL-3.0 ffmpeg build. Their licenses ask that the license text travel with the
binaries, and the GPL also asks that we say where to get ffmpeg's source. The
release workflow runs this after assembling the bundle:

    python scripts/third_party_notices.py --ffmpeg-dir ffmpeg-tmp \
        --out out/plaud-tray/PlaudTools/THIRD_PARTY_NOTICES.txt

Run it with the same Python the bundle was built from. It reads the installed
package metadata, so the versions it lists are the versions that ship.
"""

from __future__ import annotations

import argparse
import re
import sys
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT_PROJECT = "plaud-tools"
ROOT_EXTRAS = ("tray",)
COPYRIGHT = "Copyright (C) 2026 massive-value"

# File names inside a .dist-info folder that hold license or notice text.
LICENSE_FILE = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE)", re.IGNORECASE)
RULE = "=" * 78


def runtime_distributions(
    root: str = ROOT_PROJECT, extras: tuple[str, ...] = ROOT_EXTRAS
) -> list[metadata.Distribution]:
    """Every installed distribution the root project needs at runtime, root excluded."""
    found: dict[str, metadata.Distribution] = {}
    queue = [(root, extras)]
    while queue:
        name, active_extras = queue.pop()
        dist = metadata.distribution(name)
        for line in dist.requires or []:
            req = Requirement(line)
            # Markers decide both platform deps (sys_platform == "win32") and
            # extras (extra == "tray"); an empty extra means "no extra asked for".
            if req.marker and not any(req.marker.evaluate({"extra": e}) for e in (*active_extras, "")):
                continue
            key = canonicalize_name(req.name)
            if key not in found:
                found[key] = metadata.distribution(req.name)
                queue.append((req.name, tuple(req.extras)))
    found.pop(canonicalize_name(root), None)
    return sorted(found.values(), key=lambda d: canonicalize_name(d.metadata["Name"]))


def license_name(dist: metadata.Distribution) -> str:
    """Short license label: the SPDX expression, else a classifier, else the License field."""
    meta = dist.metadata
    if expression := meta.get("License-Expression"):
        return expression
    classifiers = [
        c.split(" :: ")[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License ::")
    ]
    if classifiers:
        return ", ".join(classifiers)
    field = (meta.get("License") or "").strip()
    return field.splitlines()[0] if field else "UNKNOWN"


def license_texts(dist: metadata.Distribution) -> list[str]:
    """Full text of every license/notice file the distribution installed in its .dist-info."""
    texts = []
    for file in dist.files or []:
        if file.parts[0].endswith(".dist-info") and LICENSE_FILE.match(file.name):
            texts.append(Path(dist.locate_file(file)).read_text(encoding="utf-8", errors="replace").strip())
    return texts


def python_section() -> str:
    license_file = Path(sys.base_prefix) / "LICENSE.txt"
    if not license_file.is_file():
        sys.exit(f"Python license not found at {license_file}; refusing to write incomplete notices.")
    version = ".".join(map(str, sys.version_info[:3]))
    return section(
        f"Python {version}", "PSF-2.0 and others (see below)", [license_file.read_text(encoding="utf-8")]
    )


def ffmpeg_section(ffmpeg_dir: Path) -> str:
    # Gyan's zip unpacks to ffmpeg-<version>-essentials_build/ with LICENSE and
    # README.txt (build configuration, bundled libraries, source commit) at its root.
    build_dirs = [p for p in ffmpeg_dir.rglob("LICENSE") if (p.parent / "README.txt").is_file()]
    if len(build_dirs) != 1:
        sys.exit(f"Expected exactly one ffmpeg build folder with LICENSE and README.txt under {ffmpeg_dir}.")
    build = build_dirs[0].parent
    match = re.match(r"ffmpeg-(\d[\d.]*)-", build.name)
    if not match:
        sys.exit(f"Can't read the ffmpeg version from folder name {build.name!r}.")
    version = match.group(1)
    header = (
        'Shipped as mcp\\ffmpeg.exe, the Gyan.dev "essentials" Windows build.\n'
        "PlaudTools runs it as a separate program and does not link to it.\n"
        "\n"
        "Source code:\n"
        f"  FFmpeg {version}: https://ffmpeg.org/releases/ffmpeg-{version}.tar.xz\n"
        f"  Build used: https://github.com/GyanD/codexffmpeg/releases/tag/{version}\n"
        "  The README below lists the exact FFmpeg commit, the build configuration,\n"
        "  and every library compiled in, with versions."
    )
    texts = [
        header,
        (build / "README.txt").read_text(encoding="utf-8"),
        (build / "LICENSE").read_text(encoding="utf-8"),
    ]
    return section(f"FFmpeg {version}", "GPL-3.0-or-later", texts)


def dependency_section(dist: metadata.Distribution) -> str:
    texts = license_texts(dist)
    if not texts:
        home = dist.metadata.get("Home-page") or next(
            iter(dist.metadata.get_all("Project-URL") or []), "PyPI"
        )
        texts = [f"No license file is installed with this package. See {home}."]
    return section(f"{dist.metadata['Name']} {dist.version}", license_name(dist), texts)


def section(title: str, license_label: str, texts: list[str]) -> str:
    body = "\n\n".join(t.strip() for t in texts)
    return f"{RULE}\n{title}\nLicense: {license_label}\n{RULE}\n\n{body}\n"


def build_notices(ffmpeg_dir: Path) -> str:
    dists = runtime_distributions()
    intro = (
        f"PlaudTools {metadata.version(ROOT_PROJECT)}\n"
        f"{COPYRIGHT}\n"
        "Licensed under the GNU Lesser General Public License v3.0 or later. See LICENSE.txt.\n"
        "\n"
        "This bundle also contains the third-party software below, each under its own license:\n"
        "\n"
        + "\n".join(f"  - {d.metadata['Name']} {d.version} ({license_name(d)})" for d in dists)
        + "\n  - Python runtime\n  - FFmpeg\n"
    )
    sections = [dependency_section(d) for d in dists]
    return "\n".join([intro, python_section(), ffmpeg_section(ffmpeg_dir), *sections])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--ffmpeg-dir", type=Path, required=True, help="folder the pinned ffmpeg zip was extracted to"
    )
    parser.add_argument("--out", type=Path, required=True, help="where to write THIRD_PARTY_NOTICES.txt")
    args = parser.parse_args()
    args.out.write_text(build_notices(args.ffmpeg_dir), encoding="utf-8")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
