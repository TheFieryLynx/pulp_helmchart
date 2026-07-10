import copy
import hashlib
import io
import os
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import yaml


CHART_YAML = "Chart.yaml"


class HelmChartError(ValueError):
    """Raised when a Helm chart archive cannot be parsed."""


@dataclass(frozen=True)
class ChartMetadata:
    """Metadata parsed from a packaged Helm chart archive."""

    name: str
    version: str
    api_version: str | None
    app_version: str | None
    description: str | None
    annotations: dict[str, Any] | None
    chart_yaml: dict[str, Any]


@dataclass(frozen=True)
class RepositoryChartEntry:
    """One chart archive reference parsed from a classic Helm repository index."""

    chart_name: str
    version: str
    urls: list[str]
    digest: str | None
    raw: dict[str, Any]


def utc_timestamp(value: datetime | None = None) -> str:
    """Return a Helm-compatible UTC timestamp."""
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def default_archive_filename(name: str, version: str) -> str:
    """Return Helm's conventional packaged chart filename."""
    return f"{name}-{version}.tgz"


def sha256_file(fileobj) -> str:
    """Compute the sha256 digest for a file-like object without changing its final position."""
    position = fileobj.tell()
    fileobj.seek(0)
    digest = hashlib.sha256()
    for chunk in iter(lambda: fileobj.read(1024 * 1024), b""):
        digest.update(chunk)
    fileobj.seek(position)
    return digest.hexdigest()


def parse_chart_archive(fileobj) -> ChartMetadata:
    """Parse and validate metadata from a Helm chart ``.tgz`` archive."""
    position = fileobj.tell()
    fileobj.seek(0)
    try:
        with tarfile.open(fileobj=fileobj, mode="r:gz") as archive:
            chart_member = _find_chart_yaml(archive)
            extracted = archive.extractfile(chart_member)
            if extracted is None:
                raise HelmChartError("Chart.yaml could not be read from the chart archive.")
            raw_chart_yaml = extracted.read()
    except (tarfile.TarError, OSError) as exc:
        raise HelmChartError("Uploaded file is not a valid gzip tar Helm chart archive.") from exc
    finally:
        fileobj.seek(position)

    try:
        chart_yaml = yaml.safe_load(io.BytesIO(raw_chart_yaml))
    except yaml.YAMLError as exc:
        raise HelmChartError("Chart.yaml is not valid YAML.") from exc

    if not isinstance(chart_yaml, dict):
        raise HelmChartError("Chart.yaml must be a YAML mapping.")

    name = chart_yaml.get("name")
    version = chart_yaml.get("version")
    if not name or not isinstance(name, str):
        raise HelmChartError("Chart.yaml must define a string 'name'.")
    if not version or not isinstance(version, str):
        raise HelmChartError("Chart.yaml must define a string 'version'.")

    return ChartMetadata(
        name=name,
        version=version,
        api_version=_optional_str(chart_yaml.get("apiVersion")),
        app_version=_optional_str(chart_yaml.get("appVersion")),
        description=_optional_str(chart_yaml.get("description")),
        annotations=_optional_dict(chart_yaml.get("annotations")),
        chart_yaml=_json_compatible(chart_yaml),
    )


def parse_repository_index(fileobj) -> list[RepositoryChartEntry]:
    """Parse chart entries from a classic Helm repository ``index.yaml`` file."""
    try:
        index = yaml.safe_load(fileobj)
    except yaml.YAMLError as exc:
        raise HelmChartError("Helm repository index.yaml is not valid YAML.") from exc

    if not isinstance(index, dict):
        raise HelmChartError("Helm repository index.yaml must be a YAML mapping.")
    entries = index.get("entries")
    if not isinstance(entries, dict):
        raise HelmChartError("Helm repository index.yaml must define an 'entries' mapping.")

    parsed: list[RepositoryChartEntry] = []
    for chart_name in sorted(entries):
        chart_entries = entries[chart_name]
        if not isinstance(chart_entries, list):
            raise HelmChartError(
                f"Helm repository index entry for '{chart_name}' must be a list."
            )
        for entry in chart_entries:
            if not isinstance(entry, dict):
                raise HelmChartError(
                    f"Helm repository index entry for '{chart_name}' must be a mapping."
                )
            version = entry.get("version")
            urls = entry.get("urls")
            if not version or not isinstance(version, str):
                raise HelmChartError(
                    f"Helm repository index entry for '{chart_name}' is missing string version."
                )
            if not isinstance(urls, list) or not urls or not isinstance(urls[0], str):
                raise HelmChartError(
                    f"Helm repository index entry for '{chart_name}' version '{version}' "
                    "must include at least one URL."
                )
            digest = entry.get("digest")
            parsed.append(
                RepositoryChartEntry(
                    chart_name=str(entry.get("name") or chart_name),
                    version=version,
                    urls=[str(url) for url in urls],
                    digest=str(digest) if digest else None,
                    raw=_json_compatible(entry),
                )
            )

    return parsed


def repository_index_url(remote_url: str) -> str:
    """Return the index URL for a classic Helm repository remote URL."""
    if remote_url.rstrip("/").endswith("/index.yaml"):
        return remote_url
    return urljoin(remote_url.rstrip("/") + "/", "index.yaml")


def resolve_chart_url(remote_url: str, chart_url: str) -> str:
    """Resolve a chart archive URL from an upstream Helm index entry."""
    base = remote_url if remote_url.rstrip("/").endswith("/index.yaml") else remote_url.rstrip("/") + "/"
    return urljoin(base, chart_url)


def verify_sha256_digest(expected: str | None, actual: str, url: str) -> None:
    """Verify an optional Helm index sha256 digest value."""
    if not expected:
        return
    expected = expected.removeprefix("sha256:")
    if expected != actual:
        raise HelmChartError(
            f"Digest mismatch for chart '{url}': expected sha256 {expected}, got {actual}."
        )


def index_from_entries(entries: list[dict[str, Any]], generated: str | None = None) -> str:
    """Render a deterministic classic Helm repository ``index.yaml`` document."""
    generated = generated or utc_timestamp()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        grouped.setdefault(entry["name"], []).append(_clean_entry(entry))

    rendered: dict[str, Any] = {
        "apiVersion": "v1",
        "entries": {},
        "generated": generated,
    }

    for name in sorted(grouped):
        rendered["entries"][name] = sorted(
            grouped[name],
            key=lambda item: (item.get("version") or "", item.get("digest") or ""),
            reverse=True,
        )

    return yaml.safe_dump(rendered, sort_keys=False, default_flow_style=False)


def build_index_entry(
    *,
    name: str,
    version: str,
    filename: str,
    digest: str,
    api_version: str | None = None,
    app_version: str | None = None,
    description: str | None = None,
    annotations: dict[str, Any] | None = None,
    created: str | None = None,
    chart_yaml: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one Helm ``index.yaml`` entry from stored chart metadata."""
    entry = copy.deepcopy(chart_yaml) if chart_yaml else {}
    entry.update(
        {
            "apiVersion": api_version or entry.get("apiVersion") or "v2",
            "name": name,
            "version": version,
            "urls": [filename],
            "digest": digest,
            "created": created or utc_timestamp(),
        }
    )
    if app_version is not None:
        entry["appVersion"] = app_version
    if description is not None:
        entry["description"] = description
    if annotations:
        entry["annotations"] = annotations
    return _clean_entry(entry)


def _find_chart_yaml(archive: tarfile.TarFile) -> tarfile.TarInfo:
    matches = [
        member
        for member in archive.getmembers()
        if member.isfile() and os.path.basename(member.name) == CHART_YAML
    ]
    if not matches:
        raise HelmChartError("Chart.yaml was not found in the chart archive.")
    matches.sort(key=lambda member: (member.name.count("/"), member.name))
    return matches[0]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_dict(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise HelmChartError("Chart.yaml 'annotations' must be a YAML mapping when present.")
    return _json_compatible(value)


def _clean_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if value is not None}


def _json_compatible(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_compatible(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
