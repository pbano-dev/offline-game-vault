#!/usr/bin/env python3
"""Validate Offline Game Vault schemas, fixtures, and cross-references."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError


SCHEMA_FILES = {
    "capsule": "capsule.schema.json",
    "host_contract": "host-contract.schema.json",
    "acceptance": "acceptance.schema.json",
    "receipt": "receipt.schema.json",
}

PRIVATE_PATTERNS = {
    "absolute Unix home path": re.compile(
        r"(?<![A-Za-z0-9_])/(?:home|Users)/[^/\s\"']+"
    ),
    "runtime UID path": re.compile(
        r"(?<![A-Za-z0-9_])/run/user/\d+(?:/|$)"
    ),
    "absolute Windows user path": re.compile(
        r"(?i)(?<![A-Za-z0-9_])[A-Z]:[\\/]+Users[\\/]+[^\\/\s\"']+"
    ),
    "file URI to a private home": re.compile(
        r"(?i)file:///(?:home|Users)/[^/\s\"']+"
    ),
}


class RepositoryValidation:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.errors: list[str] = []
        self.schemas: dict[str, dict[str, Any]] = {}
        self.fixture_count = 0
        self.validated_json_count = 0

    def error(self, message: str) -> None:
        self.errors.append(message)

    def relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    def load_json(self, path: Path) -> Any | None:
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            self.error(f"{self.relative(path)}: file does not exist")
        except UnicodeDecodeError as exc:
            self.error(f"{self.relative(path)}: not valid UTF-8: {exc}")
        except json.JSONDecodeError as exc:
            self.error(
                f"{self.relative(path)}:{exc.lineno}:{exc.colno}: "
                f"invalid JSON: {exc.msg}"
            )
        return None

    def load_schemas(self) -> None:
        schema_dir = self.root / "schemas"
        for key, filename in SCHEMA_FILES.items():
            path = schema_dir / filename
            schema = self.load_json(path)
            if schema is None:
                continue
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as exc:
                self.error(
                    f"{self.relative(path)}: invalid JSON Schema: {exc.message}"
                )
                continue
            self.schemas[key] = schema

    @staticmethod
    def json_path(parts: Iterable[Any]) -> str:
        result = "$"
        for part in parts:
            if isinstance(part, int):
                result += f"[{part}]"
            else:
                result += f".{part}"
        return result

    def validate_instance(
        self,
        path: Path,
        instance: Any,
        schema_key: str,
    ) -> None:
        schema = self.schemas.get(schema_key)
        if schema is None:
            self.error(
                f"{self.relative(path)}: cannot validate because schema "
                f"{SCHEMA_FILES[schema_key]} is unavailable"
            )
            return

        validator = Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        )
        found_error = False
        for issue in sorted(
            validator.iter_errors(instance),
            key=lambda error: [str(part) for part in error.absolute_path],
        ):
            found_error = True
            location = self.json_path(issue.absolute_path)
            self.error(
                f"{self.relative(path)} {location}: {issue.message}"
            )

        if not found_error:
            self.validated_json_count += 1

    def check_privacy(self, path: Path, instance: Any) -> None:
        serialized = json.dumps(instance, ensure_ascii=False)
        for label, pattern in PRIVATE_PATTERNS.items():
            match = pattern.search(serialized)
            if match:
                self.error(
                    f"{self.relative(path)}: possible {label}: "
                    f"{match.group(0)!r}"
                )

    def require_relative_file(
        self,
        fixture_dir: Path,
        value: Any,
        context: str,
    ) -> Path | None:
        if not isinstance(value, str) or not value:
            self.error(f"{context}: expected a non-empty relative path")
            return None

        candidate = fixture_dir / value
        try:
            candidate.resolve().relative_to(fixture_dir.resolve())
        except ValueError:
            self.error(
                f"{context}: path escapes fixture directory: {value!r}"
            )
            return None

        if not candidate.is_file():
            self.error(
                f"{context}: referenced file does not exist: {value!r}"
            )
            return None

        return candidate

    def check_acceptance_logic(
        self,
        path: Path,
        report: dict[str, Any],
        profile_id: str,
    ) -> None:
        tests = report.get("tests", [])
        test_ids = [
            test.get("id")
            for test in tests
            if isinstance(test, dict)
            and isinstance(test.get("id"), str)
        ]
        duplicates = sorted(
            {test_id for test_id in test_ids if test_ids.count(test_id) > 1}
        )
        if duplicates:
            self.error(
                f"{self.relative(path)}: duplicate acceptance test IDs: "
                f"{', '.join(duplicates)}"
            )

        statuses = {
            test.get("status")
            for test in tests
            if isinstance(test, dict)
            and isinstance(test.get("status"), str)
        }
        result = report.get("result")

        if result == "passed" and statuses.intersection(
            {"failed", "not_tested"}
        ):
            self.error(
                f"{self.relative(path)}: result 'passed' is inconsistent "
                "with failed or not_tested checks"
            )

        if result == "passed_with_limitations" and "failed" in statuses:
            self.error(
                f"{self.relative(path)}: result "
                "'passed_with_limitations' cannot contain failed checks"
            )

        if result == "failed" and "failed" not in statuses:
            self.error(
                f"{self.relative(path)}: result 'failed' requires at least "
                "one failed check"
            )

        report_profile = report.get("profile_id")
        if report_profile != profile_id:
            self.error(
                f"{self.relative(path)}: profile_id {report_profile!r} "
                f"does not match referencing profile {profile_id!r}"
            )

    def validate_fixture(self, capsule_path: Path) -> None:
        self.fixture_count += 1
        fixture_dir = capsule_path.parent

        capsule = self.load_json(capsule_path)
        if not isinstance(capsule, dict):
            if capsule is not None:
                self.error(
                    f"{self.relative(capsule_path)}: "
                    "top-level value must be an object"
                )
            return

        self.validate_instance(capsule_path, capsule, "capsule")
        self.check_privacy(capsule_path, capsule)

        if capsule.get("sanitized_fixture") is not True:
            self.error(
                f"{self.relative(capsule_path)}: public fixture must set "
                "'sanitized_fixture' to true"
            )

        capsule_id = capsule.get("capsule_id")

        documents = capsule.get("documents", {})
        if isinstance(documents, dict):
            for key, value in documents.items():
                self.require_relative_file(
                    fixture_dir,
                    value,
                    (
                        f"{self.relative(capsule_path)} "
                        f"$.documents.{key}"
                    ),
                )

        objects = capsule.get("objects", [])
        object_ids = [
            item.get("id")
            for item in objects
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
        ]
        duplicate_object_ids = sorted(
            {
                object_id
                for object_id in object_ids
                if object_ids.count(object_id) > 1
            }
        )
        if duplicate_object_ids:
            self.error(
                f"{self.relative(capsule_path)}: duplicate object IDs: "
                f"{', '.join(duplicate_object_ids)}"
            )
        known_objects = set(object_ids)

        profiles = capsule.get("profiles", [])
        profile_ids = [
            profile.get("id")
            for profile in profiles
            if isinstance(profile, dict)
            and isinstance(profile.get("id"), str)
        ]
        duplicate_profile_ids = sorted(
            {
                profile_id
                for profile_id in profile_ids
                if profile_ids.count(profile_id) > 1
            }
        )
        if duplicate_profile_ids:
            self.error(
                f"{self.relative(capsule_path)}: duplicate profile IDs: "
                f"{', '.join(duplicate_profile_ids)}"
            )

        referenced_acceptance: set[Path] = set()
        referenced_contracts: set[Path] = set()

        for index, profile in enumerate(profiles):
            if not isinstance(profile, dict):
                continue

            profile_id = profile.get("id", f"profiles[{index}]")
            context = (
                f"{self.relative(capsule_path)} "
                f"$.profiles[{index}] ({profile_id})"
            )

            for dependency in profile.get("dependencies", []):
                if dependency not in known_objects:
                    self.error(
                        f"{context}: dependency references unknown "
                        f"object ID {dependency!r}"
                    )

            contract_path = self.require_relative_file(
                fixture_dir,
                profile.get("host_contract"),
                f"{context}.host_contract",
            )
            if contract_path is not None:
                referenced_contracts.add(contract_path.resolve())
                contract = self.load_json(contract_path)
                if isinstance(contract, dict):
                    self.validate_instance(
                        contract_path,
                        contract,
                        "host_contract",
                    )
                    self.check_privacy(contract_path, contract)

                    if contract.get("platform") != profile.get("platform"):
                        self.error(
                            f"{self.relative(contract_path)}: platform "
                            f"{contract.get('platform')!r} does not match "
                            f"profile platform "
                            f"{profile.get('platform')!r}"
                        )

            report_name = profile.get("acceptance_report")
            if report_name is None:
                if profile.get("status") == "verified":
                    self.error(
                        f"{context}: verified profile requires "
                        "acceptance_report"
                    )
                continue

            report_path = self.require_relative_file(
                fixture_dir,
                report_name,
                f"{context}.acceptance_report",
            )
            if report_path is None:
                continue

            referenced_acceptance.add(report_path.resolve())
            report = self.load_json(report_path)
            if isinstance(report, dict):
                self.validate_instance(
                    report_path,
                    report,
                    "acceptance",
                )
                self.check_privacy(report_path, report)

                if report.get("capsule_id") != capsule_id:
                    self.error(
                        f"{self.relative(report_path)}: capsule_id "
                        f"{report.get('capsule_id')!r} does not match "
                        f"{capsule_id!r}"
                    )

                self.check_acceptance_logic(
                    report_path,
                    report,
                    str(profile_id),
                )

                if (
                    profile.get("status") == "verified"
                    and report.get("result")
                    not in {"passed", "passed_with_limitations"}
                ):
                    self.error(
                        f"{context}: verified profile requires a "
                        "passing acceptance result"
                    )

        for path in sorted(fixture_dir.glob("host-contract*.json")):
            instance = self.load_json(path)
            if isinstance(instance, dict):
                if path.resolve() not in referenced_contracts:
                    self.error(
                        f"{self.relative(path)}: host contract is not "
                        "referenced by any profile"
                    )

        for path in sorted(fixture_dir.glob("acceptance*.json")):
            instance = self.load_json(path)
            if isinstance(instance, dict):
                if path.resolve() not in referenced_acceptance:
                    self.error(
                        f"{self.relative(path)}: acceptance report is not "
                        "referenced by any profile"
                    )

        for path in sorted(fixture_dir.glob("receipt*.json")):
            instance = self.load_json(path)
            if isinstance(instance, dict):
                self.validate_instance(path, instance, "receipt")
                self.check_privacy(path, instance)

                if instance.get("capsule_id") != capsule_id:
                    self.error(
                        f"{self.relative(path)}: receipt capsule_id does "
                        "not match fixture capsule_id"
                    )

    def run(self) -> int:
        self.load_schemas()

        fixture_paths = sorted(
            (self.root / "fixtures").glob("*/capsule.json")
        )
        if not fixture_paths:
            self.error("fixtures/: no capsule.json files found")

        for capsule_path in fixture_paths:
            self.validate_fixture(capsule_path)

        if self.errors:
            print(
                f"VALIDATION FAILED: {len(self.errors)} error(s)",
                file=sys.stderr,
            )
            for issue in self.errors:
                print(f"- {issue}", file=sys.stderr)
            return 1

        print(
            "VALIDATION PASSED: "
            f"{len(self.schemas)} schemas, "
            f"{self.fixture_count} fixture(s), "
            f"{self.validated_json_count} JSON instance(s)"
        )
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate Offline Game Vault repository metadata."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root (default: inferred from this script).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return RepositoryValidation(args.root).run()


if __name__ == "__main__":
    raise SystemExit(main())
