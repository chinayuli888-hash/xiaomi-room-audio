"""Build a source-only preview from committed Git files, without local settings."""
import argparse
import hashlib
from pathlib import Path
import subprocess
from zipfile import ZipFile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    args.out.mkdir(parents=True, exist_ok=True)
    files = subprocess.check_output(["git", "ls-tree", "-rz", "--name-only", "HEAD"],
                                    cwd=root).decode("utf-8").split("\0")[:-1]
    forbidden = {"settings.json", "settings.local.json", "restore.json", ".env"}
    for name in files:
        path = Path(name)
        if (path.name in forbidden or path.suffix in {".wav", ".log", ".exe", ".pyc"} or
                path.parts[0] in {"bin", "node_modules", "work"}):
            raise SystemExit(f"Local-only or binary dependency unexpectedly tracked: {name}")
    archive = args.out / "xiaomi-room-audio-0.6-windows-source.zip"
    if archive.exists():
        raise SystemExit(f"Archive already exists: {archive}")
    subprocess.run(["git", "archive", "--format=zip", f"--output={archive.resolve()}", "HEAD"],
                   cwd=root, check=True)
    with ZipFile(archive) as z:
        if z.testzip() or {i.filename for i in z.infolist() if not i.is_dir()} != set(files):
            raise SystemExit("Archive content verification failed.")
    guide = args.out / "USAGE-0.6.md"
    guide.write_bytes((root / "USAGE.md").read_bytes())
    checksums = args.out / "SHA256SUMS.txt"
    checksums.write_text("".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in (archive, guide)
    ), encoding="utf-8")
    print(f"Verified {len(files)} committed source files; wrote archive, guide and SHA256SUMS.")


if __name__ == "__main__":
    main()
