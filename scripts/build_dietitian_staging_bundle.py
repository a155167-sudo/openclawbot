"""Assemble a minimal, deidentified Railway bundle for dietitian read staging."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.build_dietitian_staging_fixture import build_fixture

SOURCE_FILES = (
    "dietitian_health_check_staging.py",
    "dietitian_health_check_api.py",
    "vip_health_check.py",
)
RAILWAY_CONFIG = """[build]\nbuilder = \"RAILPACK\"\n\n[deploy]\nstartCommand = \"uvicorn dietitian_health_check_staging:create_app --factory --host 0.0.0.0 --port $PORT\"\nrestartPolicyType = \"ON_FAILURE\"\nrestartPolicyMaxRetries = 10\n"""
RUNTIME_REQUIREMENTS = """fastapi\nuvicorn[standard]\nrequests\n"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_bundle(destination: str | Path) -> dict[str, object]:
    output = Path(destination).resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite deployment bundle: {output}")
    output.mkdir(parents=True)
    try:
        copied: list[str] = []
        for relative_name in SOURCE_FILES:
            source = REPOSITORY_ROOT / relative_name
            if not source.is_file() or source.is_symlink():
                raise RuntimeError(f"invalid staging source file: {relative_name}")
            target = output / relative_name
            shutil.copyfile(source, target)
            copied.append(relative_name)
        (output / "railway.toml").write_text(RAILWAY_CONFIG, encoding="utf-8")
        (output / "requirements.txt").write_text(
            RUNTIME_REQUIREMENTS, encoding="utf-8"
        )
        fixture = output / "staging-data" / "deidentified.db"
        database_sha256 = build_fixture(fixture)
        image = fixture.parent / "sample-meal.jpg"
        manifest: dict[str, object] = {
            "artifact_kind": "dietitian-health-check-read-staging",
            "contains_real_customer_data": False,
            "database_sha256": database_sha256,
            "image_sha256": _sha256(image),
            "files": sorted(
                copied
                + [
                    "railway.toml",
                    "requirements.txt",
                    "staging-data/deidentified.db",
                    "staging-data/sample-meal.jpg",
                ]
            ),
        }
        (output / "artifact-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination")
    args = parser.parse_args()
    print(json.dumps(build_bundle(args.destination), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
