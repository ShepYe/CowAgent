"""Check source-launch requirements against installed distributions without network access."""
import sys
from importlib import metadata
from pathlib import Path

try:
    from packaging.requirements import Requirement
except ImportError:
    try:
        from pip._vendor.packaging.requirements import Requirement
    except ImportError:
        print("Dependency checker requires pip or packaging; run start-personal.cmd to prepare the environment.")
        sys.exit(1)


def dependency_problems(requirement_files):
    problems = []
    for requirement_file in requirement_files:
        for line in Path(requirement_file).read_text(encoding="utf-8-sig").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("--only-binary="):
                continue
            requirement = Requirement(line)
            if requirement.marker and not requirement.marker.evaluate():
                continue
            try:
                installed = metadata.version(requirement.name)
            except metadata.PackageNotFoundError:
                problems.append(f"{requirement}: missing")
                continue
            if not requirement.specifier.contains(installed, prereleases=True):
                problems.append(f"{requirement}: installed {installed}")
    return list(dict.fromkeys(problems))


if __name__ == "__main__":
    problems = dependency_problems(sys.argv[1:])
    for problem in problems:
        print(problem)
    sys.exit(bool(problems))
