import copy
import hashlib
import io
import logging
import re
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import yaml


CHART_YAML = "Chart.yaml"
# Chart.yaml is normally a small text file; these generous caps prevent tiny
# compressed uploads from expanding into unbounded metadata or tar headers.
MAX_CHART_METADATA_SIZE = 2 * 1024 * 1024
MAX_CHART_ARCHIVE_MEMBERS = 10_000
log = logging.getLogger(__name__)
_HELM_VERSION = re.compile(
    r"^v?(?P<major>[0-9]+)(?:\.(?P<minor>[0-9]+))?"
    r"(?:\.(?P<patch>[0-9]+))?"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


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
            if chart_member.size > MAX_CHART_METADATA_SIZE:
                raise HelmChartError(
                    f"Chart.yaml exceeds {MAX_CHART_METADATA_SIZE} decompressed bytes."
                )
            extracted = archive.extractfile(chart_member)
            if extracted is None:
                raise HelmChartError(
                    "Chart.yaml could not be read from the chart archive."
                )
            raw_chart_yaml = extracted.read(MAX_CHART_METADATA_SIZE + 1)
            if len(raw_chart_yaml) > MAX_CHART_METADATA_SIZE:
                raise HelmChartError(
                    f"Chart.yaml exceeds {MAX_CHART_METADATA_SIZE} decompressed bytes."
                )
    except (tarfile.TarError, OSError) as exc:
        raise HelmChartError(
            "Uploaded file is not a valid gzip tar Helm chart archive."
        ) from exc
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
    if name in {".", ".."} or any(char in name for char in ("/", "\\", "\x00")):
        raise HelmChartError(f"Chart.yaml name {name!r} is invalid.")
    try:
        parse_helm_version(version)
    except HelmChartError as exc:
        raise HelmChartError(f"Chart.yaml version {version!r} is invalid.") from exc

    return ChartMetadata(
        name=name,
        version=version,
        api_version=_optional_str(chart_yaml.get("apiVersion")),
        app_version=_optional_str(chart_yaml.get("appVersion")),
        description=_optional_str(chart_yaml.get("description")),
        annotations=_optional_dict(chart_yaml.get("annotations")),
        chart_yaml=_json_compatible(chart_yaml),
    )


def parse_helm_version(version: str) -> tuple:
    """Return Helm-compatible SemVer precedence, accepting its v/short-version coercions."""
    match = _HELM_VERSION.fullmatch(version) if isinstance(version, str) else None
    if not match:
        raise HelmChartError(f"Invalid Helm chart version {version!r}.")
    prerelease = match.group("prerelease")
    identifiers = ()
    if prerelease:
        parts = prerelease.split(".")
        if any(part.isdigit() and len(part) > 1 and part[0] == "0" for part in parts):
            raise HelmChartError(f"Invalid Helm chart version {version!r}.")
        identifiers = tuple(
            (0, int(part)) if part.isdigit() else (1, part) for part in parts
        )
    return (
        int(match.group("major")),
        int(match.group("minor") or 0),
        int(match.group("patch") or 0),
        0 if prerelease else 1,
        identifiers,
    )


def helm_version_sort_key(version: str, tie: str = "") -> tuple:
    """Place malformed legacy versions after valid SemVer with a deterministic fallback."""
    try:
        return (1, *parse_helm_version(version), version, tie)
    except HelmChartError:
        return (0, str(version), tie)


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
        raise HelmChartError(
            "Helm repository index.yaml must define an 'entries' mapping."
        )

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
            if (
                not isinstance(urls, list)
                or not urls
                or any(not isinstance(url, str) for url in urls)
            ):
                raise HelmChartError(
                    f"Helm repository index entry for '{chart_name}' version '{version}' "
                    "must include a list of URL strings."
                )
            urls = [url for url in urls if url.strip()]
            if not urls:
                raise HelmChartError(
                    f"Helm repository index entry for '{chart_name}' version '{version}' "
                    "has no non-empty chart URL."
                )
            digest = entry.get("digest")
            parsed.append(
                RepositoryChartEntry(
                    chart_name=str(entry.get("name") or chart_name),
                    version=version,
                    urls=urls,
                    digest=str(digest) if digest else None,
                    raw=_json_compatible(entry),
                )
            )

    return parsed


def repository_index_url(remote_url: str) -> str:
    """Return the index URL for a classic Helm repository remote URL."""
    parts = urlsplit(remote_url)
    if parts.path.endswith("/index.yaml"):
        return urlunsplit(parts._replace(fragment=""))

    base_path = parts.path or "/"
    if not base_path.endswith("/"):
        base_path += "/"
    base = urlunsplit((parts.scheme, parts.netloc, base_path, "", ""))
    index = urlsplit(urljoin(base, "index.yaml"))
    return urlunsplit(index._replace(query=parts.query))


def resolve_chart_url(remote_url: str, chart_url: str) -> str:
    """Resolve archives beside index.yaml without inheriting its auth query."""
    return urljoin(repository_index_url(remote_url), chart_url)


def redact_url(url: str) -> str:
    """Remove URL credentials, query parameters and fragments from diagnostics."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        if ":" in hostname:
            hostname = f"[{hostname}]"
        if parsed.port is not None:
            hostname = f"{hostname}:{parsed.port}"
        return urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))
    except ValueError:
        return "<invalid URL>"


def normalize_chart_host(host: str) -> str:
    """Validate and normalize an exact DNS hostname or IPv4 address allowlist entry."""
    if not isinstance(host, str) or not host or host != host.strip():
        raise ValueError("Chart hosts must be non-empty hostnames.")
    normalized = host.lower().rstrip(".")
    if len(normalized) > 253 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in normalized.split(".")
    ):
        raise ValueError(
            "Chart hosts must be hostnames without a scheme, path, or port."
        )
    return normalized


def validate_chart_url(
    remote_url: str,
    chart_url: str,
    allowed_chart_hosts: list[str] | None = None,
    *,
    base_url: str | None = None,
) -> str:
    """Resolve a chart or redirect URL against the remote's explicit origin trust policy."""
    try:
        source = urlsplit(remote_url)
        candidate = urlsplit(chart_url)
        resolved = (
            urljoin(base_url, chart_url)
            if base_url
            else resolve_chart_url(remote_url, chart_url)
        )
        target = urlsplit(resolved)
        if target.scheme.lower() not in {"http", "https"} or not target.hostname:
            raise ValueError("Only HTTP and HTTPS chart URLs are supported.")
        if (
            candidate.username
            or candidate.password
            or target.username
            or target.password
        ):
            raise ValueError("Credentials in chart URLs are not allowed.")
        source_origin = _http_origin(source)
        target_origin = _http_origin(target)
    except ValueError as exc:
        raise HelmChartError(
            f"Unsafe chart URL {redact_url(chart_url)!r}: {exc}"
        ) from None
    if source_origin != target_origin:
        allowed = set(allowed_chart_hosts or ())
        hostname = target.hostname.lower().rstrip(".")
        if hostname not in allowed:
            raise HelmChartError(
                f"Chart URL {redact_url(chart_url)!r} is outside the configured remote origin "
                "or allowed_chart_hosts."
            )
    return resolved


def _http_origin(parts):
    port = parts.port
    if port is None:
        port = 443 if parts.scheme.lower() == "https" else 80
    return (parts.scheme.lower(), parts.hostname.lower().rstrip("."), port)


def verify_sha256_digest(expected: str | None, actual: str, url: str) -> None:
    """Verify an optional Helm index sha256 digest value."""
    if not expected:
        return
    expected = expected.removeprefix("sha256:")
    if expected != actual:
        raise HelmChartError(
            f"Digest mismatch for chart '{url}': expected sha256 {expected}, got {actual}."
        )


def filter_repository_entries(
    entries: list[RepositoryChartEntry],
    *,
    include_charts: list[str] | None = None,
    exclude_charts: list[str] | None = None,
    include_versions: dict[str, list[str]] | list[str] | None = None,
    exclude_versions: dict[str, list[str]] | list[str] | None = None,
    auto_excluded_versions: dict[str, dict[str, Any]] | None = None,
    latest_only: bool = False,
) -> list[RepositoryChartEntry]:
    """Filter parsed Helm repository entries deterministically."""
    include_chart_names = set(include_charts or [])
    exclude_chart_names = set(exclude_charts or [])
    include_version_names = _version_filter_sets(include_versions)
    exclude_version_names = _version_filter_sets(exclude_versions)
    auto_excluded_versions = auto_excluded_versions or {}

    selected = [
        entry
        for entry in entries
        if (not include_chart_names or entry.chart_name in include_chart_names)
        and entry.chart_name not in exclude_chart_names
        and (
            (
                entry.chart_name not in include_version_names
                and "*" not in include_version_names
            )
            or entry.version
            in include_version_names.get(
                entry.chart_name, include_version_names.get("*", ())
            )
        )
        and entry.version not in exclude_version_names.get("*", ())
        and entry.version not in exclude_version_names.get(entry.chart_name, ())
        and entry.version not in auto_excluded_versions.get(entry.chart_name, {})
    ]

    if not latest_only:
        return selected

    latest_by_chart = {}
    for entry in selected:
        previous = latest_by_chart.get(entry.chart_name)
        if previous is None or helm_version_sort_key(
            entry.version
        ) > helm_version_sort_key(previous.version):
            latest_by_chart[entry.chart_name] = entry
    return list(latest_by_chart.values())


def _version_filter_sets(
    value: dict[str, list[str]] | list[str] | None,
) -> dict[str, set[str]]:
    """Accept legacy global lists as equivalent to a wildcard mapping during upgrades."""
    if value is None:
        return {}
    if isinstance(value, list):
        value = {"*": value} if value else {}
    if not isinstance(value, dict):
        raise HelmChartError(
            "Version filters must be lists or chart-to-version mappings."
        )
    return {name: set(versions) for name, versions in value.items()}


def index_from_entries(
    entries: list[dict[str, Any]], generated: str | None = None
) -> str:
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
        for item in grouped[name]:
            try:
                parse_helm_version(item.get("version"))
            except HelmChartError:
                log.warning(
                    "Publishing legacy chart %s with invalid version %r after valid versions.",
                    name,
                    item.get("version"),
                )
        rendered["entries"][name] = sorted(
            grouped[name],
            key=lambda item: helm_version_sort_key(
                item.get("version"), item.get("digest") or ""
            ),
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
    root = None
    chart_members = []
    for member_count, member in enumerate(archive, start=1):
        if member_count > MAX_CHART_ARCHIVE_MEMBERS:
            raise HelmChartError(
                f"Chart archive exceeds {MAX_CHART_ARCHIVE_MEMBERS} tar members."
            )
        path = member.name.rstrip("/") if member.isdir() else member.name
        parts = path.split("/")
        if (
            path.startswith("/")
            or len(parts) < 1
            or any(part in {"", ".", ".."} for part in parts)
            or "\\" in path
        ):
            raise HelmChartError(f"Unsafe chart archive member path {member.name!r}.")
        if root is None:
            root = parts[0]
        elif parts[0] != root:
            raise HelmChartError(
                "Chart archive must contain exactly one chart root directory."
            )
        if len(parts) == 1 and not member.isdir():
            raise HelmChartError(
                "Chart archive files must be inside the chart root directory."
            )
        if len(parts) == 2 and parts[1] == CHART_YAML and member.isfile():
            chart_members.append(member)
    if len(chart_members) != 1:
        raise HelmChartError(
            "Chart archive must contain exactly one Chart.yaml directly under its chart root."
        )
    return chart_members[0]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_dict(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise HelmChartError(
            "Chart.yaml 'annotations' must be a YAML mapping when present."
        )
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
