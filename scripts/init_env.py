"""Create local configuration once. Never display or replace credentials."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets


ROOT = Path(__file__).resolve().parents[1]
SECRET_NAMES = (
    "API_TOKEN",
    "APPROVAL_TOKEN",
    "ADMIN_TOKEN",
    "POSTGRES_PASSWORD",
    "POSTGRES_API_PASSWORD",
    "POSTGRES_WORKER_PASSWORD",
)


def generate_env(path: Path = ROOT / ".env") -> bool:
    """An exclusive, mode-0600 create also protects against parallel bootstrap runs."""
    path = Path(path)
    if path.exists() or path.is_symlink():
        return False
    values = {name: secrets.token_hex(32) for name in SECRET_NAMES}
    # Distinct capabilities must stay distinct even under a faulty RNG substitute.
    if len(set(values.values())) != len(values):
        raise RuntimeError("Credential generation did not produce distinct values")
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    lines = []
    for line in template.splitlines():
        key, separator, _ = line.partition("=")
        lines.append(f"{key}={values[key]}" if separator and key in values else line)
    content = "\n".join(lines) + "\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        # Keep any partially written file: silently overwriting it on retry is unsafe.
        raise
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=ROOT / ".env")
    args = parser.parse_args(argv)
    try:
        created = generate_env(args.path)
    except Exception as error:
        print(f"Configuration was not created ({type(error).__name__}); inspect the target locally.")
        return 1
    print("Configuration created with private permissions; credentials were not displayed."
          if created else "Existing configuration preserved; no credentials changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
