"""Shared valid and invalid import-root arguments for the isolated startup scripts."""

from pathlib import Path

from session.tests.case import SRC

PACKAGE_ROOTS = (SRC["session"], SRC["utils"])


def source_roots_cases(directory):
    folder = Path(directory) / "source-roots"
    folder.mkdir()
    joined = folder / "joined"
    namespace = joined / "agent_pm"
    namespace.mkdir(parents=True)
    for package in ("session", "utils"):
        (namespace / package).symlink_to(Path(SRC[package]) / "agent_pm" / package)
    unicode = folder / "café"
    unicode.symlink_to(joined, target_is_directory=True)
    empty = folder / "empty"
    empty.mkdir()
    unprintable = [folder / name for name in ("line\nbreak", "tab\tname")]
    for path in unprintable:
        path.mkdir()
    file = folder / "file"
    file.write_text("fixture")
    split = list(PACKAGE_ROOTS)
    valid = {
        "single distribution": [str(joined)],
        "split distributions": split,
        "reverse distributions": list(reversed(split)),
        "printable unicode symlink": [str(unicode)],
        "additional empty directory": [str(empty), *split],
    }
    invalid = {
        "null": None,
        "object": {},
        "empty list": [],
        "bare path": str(joined),
        "null element": [None],
        "numeric element": [1],
        "boolean element": [True],
        "relative path": ["relative"],
        "nul character": ["/invalid\0root"],
        "newline character": [str(unprintable[0])],
        "tab character": [str(unprintable[1])],
        "missing directory": [str(folder / "missing")],
        "regular file": [str(file)],
        "duplicate directory": [str(joined), str(joined)],
    }
    return valid, invalid
