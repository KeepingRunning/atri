"""Build a deployment bundle from an explicit allowlist, never local credentials."""
import argparse
import gzip
import hashlib
import io
from pathlib import Path
import re
import tarfile
import tomllib


ROOT = Path(__file__).resolve().parents[1]
FILES = {
    "compose.yaml": "compose.yaml",
    ".env.example": "deploy/.env.example",
    "config.toml.template": "deploy/config.toml.template",
    "personal_info.txt": "personal_info.txt",
    "deploy.sh": "scripts/deploy.sh",
    "DEPLOYMENT.md": "docs/deployment.md",
}


def build(version, destination):
    project_version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?", version) or version != project_version:
        raise ValueError("Release tag must match the project version in pyproject.toml")
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"atri-{version}-deploy.tar.gz"
    with archive.open("wb") as output:
        with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as bundle:
                for name, source in sorted(FILES.items()):
                    path = ROOT / source
                    content = path.read_bytes()
                    if name == ".env.example":
                        content, replacements = re.subn(rb"(?m)^ATRI_VERSION=[^\r\n]+",
                            f"ATRI_VERSION={version}".encode(), content)
                        if replacements != 1:
                            raise ValueError("Expected exactly one ATRI_VERSION in .env.example")
                    elif name == "compose.yaml":
                        content, replacements = re.subn(rb"\$\{ATRI_VERSION:-[^}]+\}",
                            f"${{ATRI_VERSION:-{version}}}".encode(), content)
                        if replacements != 1:
                            raise ValueError("Expected exactly one ATRI_VERSION default in compose.yaml")
                    info = tarfile.TarInfo(f"atri-{version}/{name}")
                    info.size = len(content)
                    info.mode = 0o755 if name.endswith(".sh") else 0o644
                    bundle.addfile(info, io.BytesIO(content))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (destination / "SHA256SUMS").write_text(f"{checksum}  {archive.name}\n", encoding="utf-8")
    print(archive)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    build(args.version.removeprefix("v"), args.output)
