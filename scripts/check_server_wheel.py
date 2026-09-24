"""Fail if the server wheel ships non-plugin trees or omits a migration."""

import sys
from pathlib import Path
from zipfile import ZipFile


def check_wheel(path: Path) -> None:
    with ZipFile(path) as wheel:
        members = {name for name in wheel.namelist() if not name.endswith("/")}

    unexpected = sorted(
        name
        for name in members
        if not name.startswith("pulp_helmchart/")
        and not name.split("/", 1)[0].startswith("pulp_helmchart-")
    )
    if unexpected:
        raise SystemExit(f"Unexpected server wheel members: {unexpected}")

    migrations = Path("pulp_helmchart/app/migrations")
    missing = sorted(
        str(path).replace("\\", "/")
        for path in migrations.glob("*.py")
        if str(path).replace("\\", "/") not in members
    )
    if missing:
        raise SystemExit(f"Server wheel is missing migrations: {missing}")

    if "pulp_helmchart/app/models.py" not in members:
        raise SystemExit("Server wheel is missing plugin models")

    print(f"Server wheel contents valid: {len(members)} files")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: check_server_wheel.py <wheel.whl>")
    check_wheel(Path(sys.argv[1]))
