"""Distinguish transferable attachments from their implementation resources."""
from pathlib import PurePosixPath


APPLICATION_PACKAGES = frozenset({
    '.apk', '.aab', '.apks', '.xapk', '.ipa', '.jar', '.war', '.whl', '.nupkg',
})


def application_package(logical_path: str) -> bool:
    return PurePosixPath(logical_path.replace('\\', '/')).suffix.casefold() in APPLICATION_PACKAGES


def package_member(logical_path: str) -> bool:
    # Exclamation marks denote retained archive boundaries. A folder named
    # "apk" or a package mentioned in ordinary document text is not sufficient.
    return any(application_package(part) for part in logical_path.split('!')[:-1])
