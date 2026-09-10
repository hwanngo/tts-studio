from pathlib import Path

from grpc_tools import protoc


def generate_protocol(repository_root: Path) -> None:
    protocol_root = repository_root / "packages" / "protocol"
    proto_root = protocol_root / "proto" / "tts_studio"
    source_root = protocol_root / "src" / "tts_studio_protocol"
    proto_file = proto_root / "engine" / "v1" / "engine.proto"

    source_root.mkdir(parents=True, exist_ok=True)
    result = protoc.main(
        [
            "grpc_tools.protoc",
            f"--proto_path={proto_root}",
            f"--python_out={source_root}",
            f"--grpc_python_out={source_root}",
            f"--mypy_out={source_root}",
            f"--mypy_grpc_out={source_root}",
            str(proto_file),
        ]
    )
    if result != 0:
        raise RuntimeError(f"protobuf generation failed with exit code {result}")

    for grpc_module in (
        source_root / "engine" / "v1" / "engine_pb2_grpc.py",
        source_root / "engine" / "v1" / "engine_pb2_grpc.pyi",
    ):
        generated = grpc_module.read_text(encoding="utf-8")
        absolute_import = "from engine.v1 import engine_pb2 as"
        relative_import = "from . import engine_pb2 as"
        if absolute_import not in generated:
            raise RuntimeError(f"expected generated import not found in {grpc_module}")
        grpc_module.write_text(
            generated.replace(absolute_import, relative_import), encoding="utf-8", newline="\n"
        )

    for package in (source_root, source_root / "engine", source_root / "engine" / "v1"):
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_bytes(b"")


if __name__ == "__main__":
    generate_protocol(Path(__file__).resolve().parents[1])
