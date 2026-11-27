# SPDX-License-Identifier: Apache-2.0
"""Partition selected pytest files by estimated execution time."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from sglang.test.ci.ci_register import CIRegistry, HWBackend, auto_partition


@dataclass(kw_only=True)
class Partition:
    target_directories: set[Path]
    scanned_files: set[Path]
    selected_file_paths: set[Path]
    selected_ancestor_directories: set[Path]


PARTITION = pytest.StashKey[Partition]()
ACCELERATOR = re.compile(rb"\baccelerator\b")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--auto-partition-id", type=int, required=True)
    parser.addoption("--auto-partition-size", type=int, required=True)
    parser.addoption(
        "--partition-prefilter", choices=("none", "all", "accelerator"), default="none"
    )
    parser.addoption("--partition-model-file", type=Path, required=True)


def pytest_configure(config: pytest.Config) -> None:
    partition_id = config.getoption("--auto-partition-id")
    partition_size = config.getoption("--auto-partition-size")
    if partition_size <= 0:
        raise pytest.UsageError("--auto-partition-size must be positive.")
    elif not 0 <= partition_id < partition_size:
        raise pytest.UsageError(
            f"--auto-partition-id must be in range [0, {partition_size})."
        )
    else:
        pass

    mode = config.getoption("--partition-prefilter")
    if mode == "none":
        return
    else:
        pass
    durations = read_durations(config)
    targets = {
        Path(os.path.abspath(config.invocation_params.dir / argument.split("::", 1)[0]))
        for argument in config.args
    }
    target_directories = {path for path in targets if path.is_dir()}
    scanned_files = {path for path in targets if path.is_file() and is_test_file(path)}
    for target in target_directories:
        for directory, children, filenames in os.walk(target):
            children[:] = [
                name
                for name in children
                if not name.startswith(".") and name != "__pycache__"
            ]
            scanned_files.update(
                Path(directory) / name for name in filenames if is_test_file(Path(name))
            )
    candidates = scanned_files
    if mode == "accelerator":
        directories = {
            parent
            for path in scanned_files
            for parent in path.parents
            if parent.is_relative_to(config.rootpath)
        }
        marked_directories = {
            directory
            for directory in directories
            if (directory / "conftest.py").is_file()
            and ACCELERATOR.search((directory / "conftest.py").read_bytes())
        }
        candidates = {
            path
            for path in scanned_files
            if path.relative_to(config.rootpath).as_posix() in durations
            or not marked_directories.isdisjoint(path.parents)
            or ACCELERATOR.search(path.read_bytes())
        }
    else:
        pass
    selected = partition_paths(config, candidates, durations)
    config.stash[PARTITION] = Partition(
        target_directories=target_directories,
        scanned_files=scanned_files,
        selected_file_paths=selected,
        selected_ancestor_directories={
            parent
            for path in selected
            for parent in path.parents
            if parent.is_relative_to(config.rootpath)
        },
    )


def is_test_file(path: Path) -> bool:
    return path.match("test_*.py") or path.match("*_test.py")


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    partition = config.stash.get(PARTITION, None)
    if partition is None:
        return None
    elif collection_path.is_dir():
        if collection_path not in partition.selected_ancestor_directories and any(
            collection_path.is_relative_to(root)
            for root in partition.target_directories
        ):
            return True
        else:
            return None
    elif (
        collection_path in partition.scanned_files
        and collection_path not in partition.selected_file_paths
    ):
        return True
    else:
        return None


def read_durations(config: pytest.Config) -> dict[str, float]:
    duration_path: Path = config.getoption("--partition-model-file")
    with duration_path.open(encoding="utf-8") as duration_file:
        return json.load(duration_file)


def partition_paths(
    config: pytest.Config, paths: set[Path], duration_seconds_by_file: dict[str, float]
) -> set[Path]:
    file_paths = {path.relative_to(config.rootpath).as_posix() for path in paths}
    test_files = [
        CIRegistry(
            backend=HWBackend.CPU,
            filename=file_path,
            est_time=duration_seconds_by_file.get(file_path, 1.0),
        )
        for file_path in sorted(file_paths)
    ]
    return {
        config.rootpath / test_file.filename
        for test_file in auto_partition(
            test_files,
            config.getoption("--auto-partition-id"),
            config.getoption("--auto-partition-size"),
        )
    }


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    partition = config.stash.get(PARTITION, None)
    if partition is None:
        selected_paths = partition_paths(
            config, {item.path for item in items}, read_durations(config)
        )
    else:
        # note (fengxiaohu): Pytest bypasses ignore hooks for explicit file arguments.
        selected_paths = partition.selected_file_paths
    selected_items: list[pytest.Item] = []
    deselected_items: list[pytest.Item] = []
    for item in items:
        if item.path in selected_paths:
            selected_items.append(item)
        else:
            deselected_items.append(item)
    config.hook.pytest_deselected(items=deselected_items)
    items[:] = selected_items
