# SPDX-License-Identifier: Apache-2.0
"""Calibrate pytest partition weights from per-run JUnit reports."""

import argparse
import glob
import json
import math
import xml.etree.ElementTree as ElementTree
from collections import defaultdict
from functools import cache
from pathlib import Path


@cache
def testcase_file(repository: Path, classname: str) -> str:
    components = classname.split(".")
    for length in range(len(components), 0, -1):
        path = Path(*components[:length]).with_suffix(".py")
        if (repository / path).is_file():
            return path.as_posix()
        else:
            pass
    raise ValueError(f"Cannot resolve JUnit classname {classname!r} in {repository}")


def duration_weights(
    repository: Path, runs: list[list[Path]], percentile: float
) -> dict[str, float]:
    observations: dict[str, list[float]] = defaultdict(list)
    for reports in runs:
        seconds_by_file: dict[str, float] = defaultdict(float)
        for report in reports:
            for case in ElementTree.parse(report).iter("testcase"):
                classname = case.get("classname") or case.attrib["name"]
                filename = testcase_file(repository, classname)
                seconds = float(case.attrib["time"])
                if not math.isfinite(seconds) or seconds < 0:
                    raise ValueError(
                        f"Invalid time for {classname} in {report}: {seconds}"
                    )
                else:
                    seconds_by_file[filename] += seconds
        if not seconds_by_file:
            raise ValueError(f"No testcase observations in {reports}")
        else:
            for filename, seconds in seconds_by_file.items():
                observations[filename].append(seconds)
    weights: dict[str, float] = {}
    for filename, samples in observations.items():
        samples.sort()
        index = (len(samples) - 1) * percentile / 100
        lower = math.floor(index)
        seconds = samples[lower] + (samples[math.ceil(index)] - samples[lower]) * (
            index - lower
        )
        weights[filename] = max(0.001, round(seconds, 3))
    return weights


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument(
        "--runs-dir",
        type=Path,
        help="One subdirectory per run, containing shard XML files.",
    )
    inputs.add_argument(
        "--junit-globs",
        nargs="+",
        help="One quoted glob per run, matching its shard XML files.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--existing", type=Path)
    parser.add_argument("--prune-deleted", action="store_true")
    parser.add_argument("--percentile", type=float, default=90)
    options = parser.parse_args()
    if not 0 <= options.percentile <= 100:
        parser.error("Percentile must be between zero and 100.")
    else:
        pass
    runs: list[list[Path]] = []
    if options.runs_dir:
        for directory in sorted(
            path for path in options.runs_dir.iterdir() if path.is_dir()
        ):
            # note (fengxiaohu): The final reports already include the last retry.
            runs.append(
                sorted(
                    path
                    for path in directory.rglob("*.xml")
                    if "omni-ci-retry" not in path.relative_to(directory).parts
                )
            )
    else:
        runs = [
            sorted(Path(path) for path in glob.glob(pattern, recursive=True))
            for pattern in options.junit_globs
        ]
    if not runs or not all(runs):
        parser.error("Each run must contain at least one JUnit XML report.")
    else:
        pass
    repository = Path(__file__).resolve().parents[3]
    weights: dict[str, float] = (
        json.loads(options.existing.read_text()) if options.existing else {}
    )
    weights.update(duration_weights(repository, runs, options.percentile))
    if options.prune_deleted:
        weights = {
            path: seconds
            for path, seconds in weights.items()
            if (repository / path).is_file()
        }
    else:
        pass
    options.output.write_text(json.dumps(weights, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
else:
    pass
