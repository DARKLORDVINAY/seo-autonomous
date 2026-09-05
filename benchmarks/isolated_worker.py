"""Launched with Python -I -S -B in a minimal staged directory by v3.

This Python audit boundary prevents ordinary truth/credential/network access.
It is defense in depth for trusted detector code, NOT a kernel security sandbox.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import resource
import sys
import sysconfig

MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 32 * 1024 * 1024


def constrain(stage: Path) -> None:
    sys.dont_write_bytecode = True
    roots = {stage.resolve()}
    dependency_roots = []
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        value = sysconfig.get_path(key)
        if value:
            resolved = Path(value).resolve()
            roots.add(resolved)
            if key in {"purelib", "platlib"}:
                dependency_roots.append(resolved)
    # base stdlib path on virtualenv installations is sometimes reported as the
    # virtualenv library path. Explicitly include the actual stdlib only.
    roots.add((Path(sys.base_prefix) / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}").resolve())
    forbidden = ("socket.", "subprocess.", "os.exec", "os.spawn", "os.posix_spawn")
    blocked_events = {"os.system", "os.fork", "os.forkpty", "os.putenv", "os.unsetenv",
                      "os.remove", "os.rmdir", "os.rename", "os.mkdir", "os.link", "os.symlink",
                      "os.chmod", "os.chown", "os.truncate", "ctypes.dlopen"}

    def audit(event, arguments):
        if event.startswith(forbidden) or event in blocked_events:
            raise PermissionError("Isolated fixture worker denies external capability")
        if event == "open":
            path, mode, flags = arguments
            if not isinstance(path, (str, bytes, os.PathLike)):
                raise PermissionError("Descriptor opens are not an input capability")
            if (mode and any(letter in mode for letter in "wax+")) or flags & (
                os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            ):
                raise PermissionError("Isolated worker is read only")
            resolved = Path(os.fsdecode(path)).resolve()
            if not any(resolved.is_relative_to(root) for root in roots):
                raise PermissionError("Read is outside staged observations and Python dependencies")

    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
    sys.addaudithook(audit)
    sys.path.insert(0, str(stage))
    # -S prevents startup-time .pth/sitecustomize execution. Add only the
    # dependency roots needed by the frozen detector after the audit hook exists.
    for root in dependency_roots:
        if str(root) not in sys.path:
            sys.path.append(str(root))


def probe(forbidden_path: str) -> dict[str, bool]:
    """Test-only diagnostics use a caller's dummy sentinel, never real secrets."""
    import socket
    import subprocess

    attempts = {
        "outside_read_denied": lambda: Path(forbidden_path).read_bytes(),
        "stage_write_denied": lambda: Path("unexpected-write").write_text("blocked"),
        "socket_denied": lambda: socket.socket(),
        "subprocess_denied": lambda: subprocess.run([sys.executable, "-c", "pass"], check=True),
        "environment_mutation_denied": lambda: os.putenv("PRODUCTION_ENABLED", "true"),
    }
    results = {}
    for name, attempt in attempts.items():
        try:
            attempt()
        except PermissionError:
            results[name] = True
        else:
            results[name] = False
    results["credential_environment_absent"] = not any(
        word in key.upper() for key in os.environ for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "CREDENTIAL")
    )
    return results


def main() -> None:
    stage = Path(__file__).resolve().parent
    constrain(stage)
    if len(sys.argv) == 3 and sys.argv[1] == "--check-isolation":
        result = probe(sys.argv[2])
    else:
        from backend.app.seo.benchmark_runtime import predict_cases

        path = Path(sys.argv[1]).resolve()
        if path != stage / "observations.json":
            raise ValueError("Only staged observations are accepted")
        if path.stat().st_size > MAX_INPUT_BYTES:
            raise ValueError("Input byte budget exceeded")
        raw = path.read_bytes()
        cases = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
        result = predict_cases(cases)
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode()) > MAX_OUTPUT_BYTES:
        raise ValueError("Output byte budget exceeded")
    print(encoded)


if __name__ == "__main__":
    main()
