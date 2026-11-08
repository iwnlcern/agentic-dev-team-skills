#!/usr/bin/env python3
"""Emit or check discovery artifacts from the actual frozen implementation graph."""
import argparse
from pathlib import Path

from relay_engine import api_definitions, api_protocol, api_registry, api_schema, errors

TOOLS_DIR = Path(__file__).resolve().parent
GENERATED_PREFIX = 'tools/relay_engine/generated/api/'


def build_registry():
    return api_registry.compile_registry(api_definitions.discovery_definitions(),
                                         api_protocol.discovery_handlers(), (errors.api_error_catalog(),))


def generate(tools_dir: Path) -> dict[str, bytes]:
    # The root selects destinations only; it never contributes to schema bytes.
    artifact = api_schema.export_schema(build_registry(), api_definitions.PROTOCOL)
    return {GENERATED_PREFIX + 'schema-3.0.json': artifact.canonical_bytes}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--write', action='store_true')
    mode.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    differing = []
    for name, body in sorted(generate(TOOLS_DIR).items()):
        if not name.startswith(GENERATED_PREFIX) or Path(name).name != name[len(GENERATED_PREFIX):]:
            raise ValueError('invalid_generated_destination')
        path = TOOLS_DIR.parent / name
        if args.write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        elif not path.is_file() or path.read_bytes() != body:
            differing.append(name)
    for name in differing:
        print(name)
    return int(bool(differing))


if __name__ == '__main__':
    raise SystemExit(main())
