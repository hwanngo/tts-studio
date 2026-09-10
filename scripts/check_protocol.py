import shutil
import tempfile
from pathlib import Path

from generate_protocol import generate_protocol


def generated_files(root: Path) -> dict[Path, bytes]:
    source_root = root / "packages" / "protocol" / "src" / "tts_studio_protocol"
    return {
        path.relative_to(source_root): path.read_bytes().replace(b"\r\n", b"\n")
        for path in sorted(source_root.rglob("*"))
        if path.suffix in {".py", ".pyi"}
    }


def main() -> int:
    repository_root = Path(__file__).resolve().parents[1]
    protocol_root = repository_root / "packages" / "protocol"

    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_root = Path(temporary_directory)
        temporary_protocol = temporary_root / "packages" / "protocol"
        shutil.copytree(protocol_root / "proto", temporary_protocol / "proto")
        generate_protocol(temporary_root)

        committed = generated_files(repository_root)
        expected = generated_files(temporary_root)

    if committed == expected:
        return 0

    for path in sorted(committed.keys() | expected.keys()):
        if committed.get(path) != expected.get(path):
            print(f"generated protocol drift: {path}")
    print("run `uv run python scripts/generate_protocol.py` and commit the results")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
