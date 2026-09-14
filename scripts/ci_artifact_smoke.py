"""Exercise CI's built Core wheel outside the checkout on every supported OS."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


def smoke(dist_dir: Path) -> None:
    dist_dir = dist_dir.resolve(strict=True)
    wheels = []
    for name in ("tts_studio", "tts_studio_protocol"):
        matches = list(dist_dir.glob(f"{name}-*.whl"))
        if len(matches) != 1:
            raise RuntimeError(f"expected exactly one {name} wheel, found {matches}")
        wheels.append(str(matches[0]))
    env = os.environ.copy()
    for key in ("PYTHONPATH", "MYPYPATH", "VIRTUAL_ENV", "TTS_STUDIO_API_TOKEN_ENV"):
        env.pop(key, None)
    with tempfile.TemporaryDirectory(prefix="tts-ci-artifact-") as temporary:
        root = Path(temporary)
        environment = root / "installed"
        executable_dir = environment / ("Scripts" if os.name == "nt" else "bin")
        python = executable_dir / ("python.exe" if os.name == "nt" else "python")
        tts = executable_dir / ("tts.exe" if os.name == "nt" else "tts")

        def run(*command: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                command,
                cwd=root,
                env=env,
                check=True,
                text=True,
                capture_output=True,
                timeout=120,
            )

        try:
            run("uv", "venv", "--python", sys.executable, str(environment))
            # Explicit local wheels defeat same-version registry/cache substitution.
            # Third-party dependencies use the cross-platform root lock export.
            run(
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "--requirements",
                str(dist_dir / "core-requirements.txt"),
                *wheels,
            )
            run("uv", "pip", "check", "--python", str(python))
            run(
                str(python),
                "-I",
                "-c",
                "import importlib.util,pathlib,sys,tts_studio; "
                "assert pathlib.Path(tts_studio.__file__).is_relative_to(sys.prefix); "
                "assert all(importlib.util.find_spec(name) is None for name in "
                "('tts_studio_worker_sdk','tts_studio_fake_worker','tts_studio_vieneu_worker',"
                "'tts_studio_openai_worker','vieneu'))",
            )
            for command in ((), ("runtime",), ("service",)):
                result = run(str(tts), *command, "--help")
                assert "Usage" in result.stdout
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f"{error.cmd}:\n{error.stdout}\n{error.stderr}") from error

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        # Use a file, not a PIPE that can fill and hang a failing server.
        with (root / "server.log").open("w+") as log:
            server = subprocess.Popen(
                [
                    str(tts),
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--data-dir",
                    str(root / "data"),
                ],
                cwd=root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 20
                while True:
                    try:
                        with urlopen(f"{base_url}/api/v1/system", timeout=1) as response:
                            assert json.load(response)["status"] == "healthy"
                        break
                    except URLError:
                        if server.poll() is not None or time.monotonic() >= deadline:
                            log.seek(0)
                            raise RuntimeError(
                                f"installed Core did not become ready:\n{log.read()}"
                            )
                        time.sleep(0.1)
                with urlopen(base_url, timeout=5) as response:
                    page = response.read().decode()
                    assert "TTS Studio" in page
                assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', page)
                assert assets, "built Web asset references are missing"
                for asset in assets:
                    with urlopen(base_url + asset, timeout=5) as response:
                        assert response.status == 200
                        assert response.read(), asset
                assert "healthy" in run(str(tts), "status", "--url", base_url).stdout
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
    print("Installed artifact CLI, dependency isolation, API, and Web smoke passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, required=True)
    smoke(parser.parse_args().dist_dir)
