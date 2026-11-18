"""Build a source-only preview from committed Git files, without local settings."""
import argparse
import hashlib
from pathlib import Path
import re
import subprocess
from zipfile import ZipFile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--version", required=True, help="version used in the source archive and guide names")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.]+)?", args.version):
        parser.error("--version must be a semantic version, for example 0.6.1")
    root = Path(__file__).resolve().parents[1]
    args.out.mkdir(parents=True, exist_ok=True)
    files = subprocess.check_output(["git", "ls-tree", "-rz", "--name-only", "HEAD"],
                                    cwd=root).decode("utf-8").split("\0")[:-1]
    forbidden = {"settings.json", "settings.local.json", "speaker_profiles.json", "restore.json", ".env"}
    for name in files:
        path = Path(name)
        if (path.name in forbidden or path.name.startswith(".env.") or
                path.suffix in {".wav", ".flac", ".mp3", ".m4a", ".mp4", ".log", ".exe", ".dll", ".pyc"} or
                path.parts[0] in {"bin", "node_modules", "work"}):
            raise SystemExit(f"Local-only or binary dependency unexpectedly tracked: {name}")
    archive = args.out / f"xiaomi-room-audio-{args.version}-windows-source.zip"
    if archive.exists():
        raise SystemExit(f"Archive already exists: {archive}")
    subprocess.run(["git", "archive", "--format=zip", f"--output={archive.resolve()}", "HEAD"],
                   cwd=root, check=True)
    with ZipFile(archive) as z:
        if z.testzip() or {i.filename for i in z.infolist() if not i.is_dir()} != set(files):
            raise SystemExit("Archive content verification failed.")
        # Keep the separately downloadable guide identical to the committed package.
        guide = args.out / f"USAGE-{args.version}.md"
        guide.write_bytes(z.read("USAGE.md"))
        assets = [archive, guide]
        validation_name = f"docs/VALIDATION-{args.version}.md"
        if validation_name in z.namelist():
            validation = args.out / f"VALIDATION-{args.version}.md"
            validation.write_bytes(z.read(validation_name))
            assets.append(validation)
    checksums = args.out / "SHA256SUMS.txt"
    checksums.write_text("".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in assets
    ), encoding="utf-8")
    print(f"Verified {len(files)} committed source files; wrote {len(assets)} assets and SHA256SUMS.")


if __name__ == "__main__":
    main()
