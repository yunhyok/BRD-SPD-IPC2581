"""Collect the license notices for runtime dependencies into a distributable folder.

The collector reads installed distribution metadata instead of carrying copied third-party
trees in the repository.  It is run by build.ps1 immediately before packaging.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import shutil
import sys
from pathlib import Path


RUNTIME_DISTRIBUTIONS = ("lxml", "shapely", "numpy", "cryptography", "cffi", "pycparser", "pyinstaller")
SOURCE_URLS = {
    "libxml2": "https://download.gnome.org/sources/libxml2/",
    "libxslt": "https://download.gnome.org/sources/libxslt/",
    "python": "https://www.python.org/downloads/source/",
}


def _is_notice(path: Path) -> bool:
    name = path.name.lower()
    return (
        "license" in name
        or "copying" in name
        or "notice" in name
        or "authors" in name
        or any(part.lower() in {"license", "licenses", "legal"} for part in path.parts)
    )


def _copy(source: Path, destination: Path, output_root: Path) -> dict[str, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return {
        "path": str(destination.relative_to(output_root)).replace("\\", "/"),
        "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
    }


def collect(output: Path) -> dict[str, object]:
    repository_root = Path(__file__).resolve().parents[1]
    build_root = (repository_root / "build").resolve()
    output = output.resolve()
    if output == build_root or build_root not in output.parents:
        raise ValueError(f"License output must be a subdirectory of {build_root}")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    components: list[dict[str, object]] = []

    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise FileNotFoundError(f"Python license not found: {python_license}")
    py_dest = output / f"Python-{sys.version_info.major}.{sys.version_info.minor}" / "LICENSE.txt"
    components.append({
        "name": "Python",
        "version": sys.version.split()[0],
        "source_url": SOURCE_URLS["python"],
        "notices": [_copy(python_license, py_dest, output)],
    })

    for name in RUNTIME_DISTRIBUTIONS:
        distribution = metadata.distribution(name)
        root = distribution.locate_file("")
        destination_root = output / f"{distribution.metadata['Name']}-{distribution.version}"
        notices: list[dict[str, str]] = []
        seen: set[Path] = set()
        for relative in distribution.files or ():
            relative_path = Path(str(relative))
            if not _is_notice(relative_path):
                continue
            source = Path(distribution.locate_file(relative))
            if source.is_file() and source not in seen:
                seen.add(source)
                notices.append(_copy(source, destination_root / relative_path, output))
        # Some wheels omit notice files from RECORD. Search only their metadata directory.
        for metadata_dir in root.glob(f"{distribution.metadata['Name'].replace('-', '_')}-*.dist-info"):
            for source in metadata_dir.rglob("*"):
                if source.is_file() and _is_notice(source.relative_to(metadata_dir)) and source not in seen:
                    seen.add(source)
                    notices.append(_copy(source, destination_root / "metadata" / source.relative_to(metadata_dir), output))
        if not notices:
            raise FileNotFoundError(f"No license notice found for installed distribution {name}")
        components.append({
            "name": distribution.metadata["Name"],
            "version": distribution.version,
            # PyPI's immutable version page identifies the matching source distribution.
            "source_url": f"https://pypi.org/project/{distribution.metadata['Name']}/{distribution.version}/",
            "notices": notices,
        })

    try:
        import shapely

        geos_version = shapely.geos_version_string
    except (AttributeError, ImportError):
        geos_version = "unknown"
    components.extend([
        {"name": "GEOS", "version": geos_version, "source_url": f"https://download.osgeo.org/geos/geos-{geos_version}.tar.bz2", "notice": "See Shapely notice above."},
        {"name": "libxml2", "source_url": SOURCE_URLS["libxml2"], "notice": "See lxml notice above."},
        {"name": "libxslt", "source_url": SOURCE_URLS["libxslt"], "notice": "See lxml notice above."},
    ])
    manifest = {"generated_by": "packaging/collect_licenses.py", "components": components}
    (output / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect installed runtime dependency licenses")
    parser.add_argument("--output", type=Path, default=Path("build/third-party-licenses"))
    args = parser.parse_args()
    manifest = collect(args.output.resolve())
    notice_count = sum(len(item.get("notices", [])) for item in manifest["components"])
    print(f"Collected {notice_count} notice files in {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
