import io
import shutil
import subprocess
import tarfile

import pytest
import yaml

from pulp_helmchart import helm
from pulp_helmchart.helm import (
    HelmChartError,
    RepositoryChartEntry,
    filter_repository_entries,
    index_from_entries,
    parse_chart_archive,
    parse_repository_index,
    normalize_chart_host,
    parse_helm_version,
    redact_url,
    repository_index_url,
    resolve_chart_url,
    validate_chart_url,
    verify_sha256_digest,
)


def test_parse_chart_archive_reads_chart_yaml_metadata():
    archive = _chart_archive(
        {
            "apiVersion": "v2",
            "name": "gpu-operator",
            "version": "v26.3.3",
            "appVersion": "26.3.3",
            "description": "NVIDIA GPU Operator",
            "annotations": {"catalog.cattle.io/display-name": "GPU Operator"},
        }
    )

    metadata = parse_chart_archive(archive)

    assert metadata.name == "gpu-operator"
    assert metadata.version == "v26.3.3"
    assert metadata.api_version == "v2"
    assert metadata.app_version == "26.3.3"
    assert metadata.description == "NVIDIA GPU Operator"
    assert metadata.annotations == {"catalog.cattle.io/display-name": "GPU Operator"}


def test_parse_chart_archive_rejects_missing_required_metadata():
    archive = _chart_archive({"apiVersion": "v2", "name": "gpu-operator"})

    with pytest.raises(HelmChartError, match="version"):
        parse_chart_archive(archive)


def test_parse_chart_archive_rejects_non_tar_gzip():
    with pytest.raises(HelmChartError, match="valid gzip tar"):
        parse_chart_archive(io.BytesIO(b"not a chart"))


@pytest.mark.parametrize(
    "version",
    ["1.18.0", "v26.3.3", "4.12.0-beta.0", "25.3.0-rc.5", "1.0.0-techpreview.1", "1.2"],
)
def test_helm_compatible_versions_are_accepted(version):
    assert (
        parse_chart_archive(
            _chart_archive(
                {"apiVersion": "v2", "name": "gpu-operator", "version": version}
            )
        ).version
        == version
    )


@pytest.mark.parametrize("version", ["banana", "1.0.0-01", "1.0.0+bad_build"])
def test_helm_invalid_versions_are_rejected(version):
    with pytest.raises(HelmChartError, match="version"):
        parse_chart_archive(
            _chart_archive(
                {"apiVersion": "v2", "name": "gpu-operator", "version": version}
            )
        )


@pytest.mark.parametrize(
    "members",
    [
        ["gpu-operator/nested/Chart.yaml"],
        ["../gpu-operator/Chart.yaml"],
        ["/gpu-operator/Chart.yaml"],
        ["gpu-operator/Chart.yaml", "other/Chart.yaml"],
        ["Chart.yaml"],
    ],
)
def test_chart_archive_rejects_invalid_root_structure(members):
    with pytest.raises(HelmChartError):
        parse_chart_archive(_archive_members(members))


def test_chart_archive_allows_helm_root_name_difference():
    assert (
        parse_chart_archive(_archive_members(["other/Chart.yaml"])).name
        == "gpu-operator"
    )


def test_chart_archive_rejects_malformed_yaml():
    with pytest.raises(HelmChartError, match="valid YAML"):
        parse_chart_archive(
            _archive_members(["gpu-operator/Chart.yaml"], b"name: [unterminated")
        )


def test_chart_yaml_decompressed_size_boundary(monkeypatch):
    small = _chart_archive(
        {"apiVersion": "v2", "name": "gpu-operator", "version": "1.0.0"}
    )
    with tarfile.open(fileobj=small, mode="r:gz") as archive:
        base_size = archive.getmember("gpu-operator/Chart.yaml").size
    accepted = _chart_archive(
        {
            "apiVersion": "v2",
            "name": "gpu-operator",
            "version": "1.0.0",
            "description": "x" * 100,
        }
    )
    with tarfile.open(fileobj=accepted, mode="r:gz") as archive:
        limit = archive.getmember("gpu-operator/Chart.yaml").size
    assert limit > base_size
    monkeypatch.setattr(helm, "MAX_CHART_METADATA_SIZE", limit)
    assert parse_chart_archive(accepted).name == "gpu-operator"
    with pytest.raises(HelmChartError, match="decompressed bytes"):
        parse_chart_archive(
            _chart_archive(
                {
                    "apiVersion": "v2",
                    "name": "gpu-operator",
                    "version": "1.0.0",
                    "description": "x" * 101,
                }
            )
        )


def test_chart_archive_member_count_boundary(monkeypatch):
    monkeypatch.setattr(helm, "MAX_CHART_ARCHIVE_MEMBERS", 3)
    members = ["gpu-operator/Chart.yaml", "gpu-operator/a", "gpu-operator/b"]
    assert parse_chart_archive(_archive_members(members)).name == "gpu-operator"
    with pytest.raises(HelmChartError, match="tar members"):
        parse_chart_archive(_archive_members([*members, "gpu-operator/c"]))


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm CLI is optional")
@pytest.mark.parametrize(
    "version,valid",
    [("1.18.0", True), ("v26.3.3", True), ("4.12.0-beta.0", True), ("banana", False)],
)
def test_chart_version_validation_matches_helm_cli(tmp_path, version, valid):
    archive = _chart_archive(
        {"apiVersion": "v2", "name": "gpu-operator", "version": version}
    )
    path = tmp_path / "gpu-operator.tgz"
    path.write_bytes(archive.getvalue())
    result = subprocess.run(
        ["helm", "show", "chart", str(path)], capture_output=True, text=True
    )
    assert (result.returncode == 0) == valid
    if valid:
        assert parse_chart_archive(archive).version == version
    else:
        with pytest.raises(HelmChartError):
            parse_chart_archive(archive)


def test_index_from_entries_groups_and_sorts_deterministically():
    rendered = index_from_entries(
        [
            {
                "apiVersion": "v2",
                "name": "zeta",
                "version": "1.0.0",
                "urls": ["zeta-1.0.0.tgz"],
                "digest": "b",
                "created": "2026-01-01T00:00:00.000000Z",
            },
            {
                "apiVersion": "v2",
                "name": "alpha",
                "version": "2.0.0",
                "urls": ["alpha-2.0.0.tgz"],
                "digest": "c",
                "created": "2026-01-01T00:00:00.000000Z",
            },
            {
                "apiVersion": "v2",
                "name": "alpha",
                "version": "1.0.0",
                "urls": ["alpha-1.0.0.tgz"],
                "digest": "a",
                "created": "2026-01-01T00:00:00.000000Z",
            },
        ],
        generated="2026-01-02T00:00:00.000000Z",
    )

    parsed = yaml.safe_load(rendered)
    assert list(parsed["entries"]) == ["alpha", "zeta"]
    assert [entry["version"] for entry in parsed["entries"]["alpha"]] == [
        "2.0.0",
        "1.0.0",
    ]
    assert parsed["entries"]["alpha"][0]["urls"] == ["alpha-2.0.0.tgz"]
    assert parsed["generated"] == "2026-01-02T00:00:00.000000Z"


def test_index_versions_follow_semver_and_preserve_display_strings(caplog):
    versions = [
        "1.9.0",
        "1.10.0",
        "1.0.0-rc.1",
        "1.0.0",
        "1.0.0-rc.2",
        "v26.3.2",
        "v26.3.3",
        "4.12.0-beta.0",
        "4.12.0",
        "banana",
    ]
    entries = [
        {
            "name": "sample",
            "version": version,
            "digest": str(i),
            "urls": [f"sample-{version}.tgz"],
        }
        for i, version in enumerate(versions)
    ]
    rendered = yaml.safe_load(
        index_from_entries(entries, generated="2026-01-01T00:00:00Z")
    )
    assert [entry["version"] for entry in rendered["entries"]["sample"]] == [
        "v26.3.3",
        "v26.3.2",
        "4.12.0",
        "4.12.0-beta.0",
        "1.10.0",
        "1.9.0",
        "1.0.0",
        "1.0.0-rc.2",
        "1.0.0-rc.1",
        "banana",
    ]
    assert "legacy chart" in caplog.text


def test_semver_build_metadata_has_equal_precedence():
    assert parse_helm_version("1.0.0+build.1") == parse_helm_version("v1.0.0+build.2")


def test_parse_repository_index_reads_chart_entries():
    index = io.StringIO(
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "entries": {
                    "gpu-operator": [
                        {
                            "apiVersion": "v2",
                            "name": "gpu-operator",
                            "version": "v26.3.3",
                            "urls": ["gpu-operator-v26.3.3.tgz"],
                            "digest": "abc123",
                        }
                    ]
                },
            }
        )
    )

    entries = parse_repository_index(index)

    assert len(entries) == 1
    assert entries[0].chart_name == "gpu-operator"
    assert entries[0].version == "v26.3.3"
    assert entries[0].urls == ["gpu-operator-v26.3.3.tgz"]
    assert entries[0].digest == "abc123"


def test_parse_repository_index_discards_empty_urls_but_keeps_fallback():
    entries = parse_repository_index(
        io.StringIO(
            yaml.safe_dump(
                {
                    "entries": {
                        "sample": [{"version": "1.0.0", "urls": ["", "working.tgz"]}]
                    }
                }
            )
        )
    )
    assert entries[0].urls == ["working.tgz"]
    with pytest.raises(HelmChartError, match="URL strings"):
        parse_repository_index(
            io.StringIO(
                yaml.safe_dump(
                    {
                        "entries": {
                            "sample": [
                                {"version": "1.0.0", "urls": [None, "working.tgz"]}
                            ]
                        }
                    }
                )
            )
        )


def test_repository_index_and_chart_urls_are_resolved():
    remote = "https://helm.ngc.nvidia.com/nvidia"

    assert (
        repository_index_url(remote) == "https://helm.ngc.nvidia.com/nvidia/index.yaml"
    )
    assert (
        resolve_chart_url(remote, "gpu-operator-v26.3.3.tgz")
        == "https://helm.ngc.nvidia.com/nvidia/gpu-operator-v26.3.3.tgz"
    )
    assert (
        resolve_chart_url(remote, "https://example.com/charts/gpu-operator-v26.3.3.tgz")
        == "https://example.com/charts/gpu-operator-v26.3.3.tgz"
    )


def test_verify_sha256_digest_rejects_mismatch():
    with pytest.raises(HelmChartError, match="Digest mismatch"):
        verify_sha256_digest(
            "sha256:expected", "actual", "https://example.test/chart.tgz"
        )


def test_filter_repository_entries_includes_selected_charts_only():
    entries = _repository_entries()

    selected = filter_repository_entries(entries, include_charts=["gpu-operator"])

    assert [(entry.chart_name, entry.version) for entry in selected] == [
        ("gpu-operator", "v26.3.3"),
        ("gpu-operator", "v26.3.2"),
    ]


def test_filter_repository_entries_excludes_after_include():
    entries = _repository_entries()

    selected = filter_repository_entries(
        entries,
        include_charts=["gpu-operator", "blocked"],
        exclude_charts=["blocked"],
    )

    assert [(entry.chart_name, entry.version) for entry in selected] == [
        ("gpu-operator", "v26.3.3"),
        ("gpu-operator", "v26.3.2"),
    ]


def test_filter_repository_entries_latest_only_chooses_semantic_latest():
    entries = _repository_entries()

    selected = filter_repository_entries(
        entries, include_charts=["gpu-operator"], latest_only=True
    )

    assert [(entry.chart_name, entry.version) for entry in selected] == [
        ("gpu-operator", "v26.3.3"),
    ]


def test_latest_only_ignores_upstream_order_and_applies_filters_first():
    entries = [
        RepositoryChartEntry("sample", version, [f"{version}.tgz"], None, {})
        for version in ("1.9.0", "1.0.0-rc.1", "1.10.0", "1.0.0", "1.0.0-rc.2")
    ]
    assert [
        entry.version for entry in filter_repository_entries(entries, latest_only=True)
    ] == ["1.10.0"]
    selected = filter_repository_entries(
        entries,
        include_versions={"*": ["1.9.0", "1.10.0", "1.0.0-rc.2"]},
        exclude_versions={"sample": ["1.10.0"]},
        auto_excluded_versions={"sample": {"1.9.0": {}}},
        latest_only=True,
    )
    assert [entry.version for entry in selected] == ["1.0.0-rc.2"]


def test_filter_repository_entries_excludes_versions_after_include():
    entries = _repository_entries()

    selected = filter_repository_entries(
        entries,
        include_charts=["gpu-operator"],
        include_versions={"gpu-operator": ["v26.3.3", "v26.3.2"]},
        exclude_versions={"gpu-operator": ["v26.3.2"]},
    )

    assert [(entry.chart_name, entry.version) for entry in selected] == [
        ("gpu-operator", "v26.3.3"),
    ]


@pytest.mark.parametrize(
    "filters, expected",
    [
        ({"include_versions": {"alertmanager": ["1.18.0"]}}, [0, 2]),
        ({"include_versions": {"alertmanager": []}}, [2]),
        ({"exclude_versions": {"alertmanager": ["1.18.0"]}}, [1, 2]),
        ({"auto_excluded_versions": {"alertmanager": {"1.18.0": {}}}}, [1, 2]),
        ({"include_versions": {}, "exclude_versions": {}}, [0, 1, 2]),
        ({"include_versions": {"*": ["1.17.0"]}}, [1]),
        ({"include_versions": {"*": ["1.17.0"], "alertmanager": ["1.18.0"]}}, [0]),
        ({"include_versions": {"*": ["1.18.0"], "alertmanager": []}}, [2]),
        ({"exclude_versions": {"*": ["1.18.0"]}}, [1]),
        ({"exclude_versions": {"*": ["1.18.0"], "alertmanager": ["1.17.0"]}}, []),
        ({"exclude_versions": {"*": ["1.18.0"], "alertmanager": []}}, [1]),
        (
            {
                "include_versions": {"alertmanager": ["1.18.0"]},
                "exclude_versions": {"alertmanager": ["1.18.0"]},
            },
            [2],
        ),
        (
            {
                "auto_excluded_versions": {"alertmanager": {"1.18.0": {}}},
                "latest_only": True,
            },
            [1, 2],
        ),
        (
            {
                "include_charts": ["alertmanager"],
                "exclude_charts": ["alertmanager"],
                "include_versions": {"alertmanager": ["1.18.0"]},
            },
            [],
        ),
    ],
)
def test_version_filters_are_scoped_to_chart(filters, expected):
    entries = [
        RepositoryChartEntry(name, version, ["chart.tgz"], None, {})
        for name, version in [
            ("alertmanager", "1.18.0"),
            ("alertmanager", "1.17.0"),
            ("other-chart", "1.18.0"),
        ]
    ]
    assert filter_repository_entries(entries, **filters) == [
        entries[i] for i in expected
    ]


@pytest.mark.parametrize("field", ["include_versions", "exclude_versions"])
def test_legacy_global_filter_matches_wildcard_mapping(field):
    entries = [
        RepositoryChartEntry(name, version, ["chart.tgz"], None, {})
        for name in ("alertmanager", "other-chart")
        for version in ("1.0.0", "2.0.0", "3.0.0")
    ]
    legacy = filter_repository_entries(entries, **{field: ["1.0.0", "2.0.0"]})
    mapped = filter_repository_entries(entries, **{field: {"*": ["1.0.0", "2.0.0"]}})
    assert legacy == mapped


def test_chart_url_policy_allows_relative_and_same_origin():
    base = "https://charts.example:443/repo/"
    assert (
        validate_chart_url(base, "chart.tgz")
        == "https://charts.example:443/repo/chart.tgz"
    )
    assert (
        validate_chart_url(base, "https://charts.example/repo/chart.tgz")
        == "https://charts.example/repo/chart.tgz"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://other.example/chart.tgz",
        "http://169.254.169.254/latest/meta-data/",
        "//internal-service/chart.tgz",
        "file:///etc/passwd",
        "ftp://charts.example/chart.tgz",
        "http://charts.example/chart.tgz",
    ],
)
def test_chart_url_policy_rejects_untrusted_origins_and_schemes(url):
    with pytest.raises(HelmChartError):
        validate_chart_url("https://charts.example/repo/", url)


def test_chart_url_policy_exact_allowlisted_host():
    remote = "https://prometheus-community.github.io/helm-charts/"
    allowed = ["github.com"]
    chart = "https://github.com/prometheus-community/chart.tgz"
    assert validate_chart_url(remote, chart, allowed) == chart
    for untrusted in (
        "https://github.com.attacker.example/chart.tgz",
        "https://notgithub.com/chart.tgz",
        "http://169.254.169.254/meta-data/",
        "http://127.0.0.1/secret",
        "file://github.com/etc/passwd",
        "ftp://github.com/chart.tgz",
    ):
        with pytest.raises(HelmChartError):
            validate_chart_url(remote, untrusted, allowed)


def test_chart_host_validation_and_case_insensitive_matching():
    assert normalize_chart_host("GitHub.COM") == "github.com"
    assert (
        validate_chart_url(
            "https://example.com/", "https://GITHUB.com/chart.tgz", ["github.com"]
        )
        == "https://GITHUB.com/chart.tgz"
    )
    for invalid in (
        "",
        "https://github.com",
        "github.com/path",
        "github.com?x=y",
        "*.github.com",
        "github.com:443",
    ):
        with pytest.raises(ValueError):
            normalize_chart_host(invalid)


def test_chart_url_diagnostics_redact_credentials_and_query():
    secret_url = "https://user:secret@charts.example/chart.tgz?token=private#part"
    assert redact_url(secret_url) == "https://charts.example/chart.tgz"
    with pytest.raises(HelmChartError) as raised:
        validate_chart_url("https://charts.example/repo/", secret_url)
    assert "secret" not in str(raised.value)
    assert "private" not in str(raised.value)


def _repository_entries():
    index = io.StringIO(
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "entries": {
                    "blocked": [
                        {
                            "version": "1.0.0",
                            "urls": ["blocked-1.0.0.tgz"],
                        }
                    ],
                    "gpu-operator": [
                        {
                            "version": "v26.3.3",
                            "urls": ["gpu-operator-v26.3.3.tgz"],
                        },
                        {
                            "version": "v26.3.2",
                            "urls": ["gpu-operator-v26.3.2.tgz"],
                        },
                    ],
                },
            }
        )
    )
    return parse_repository_index(index)


def _chart_archive(chart_yaml):
    result = io.BytesIO()
    payload = yaml.safe_dump(chart_yaml).encode()
    with tarfile.open(fileobj=result, mode="w:gz") as archive:
        info = tarfile.TarInfo("gpu-operator/Chart.yaml")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    result.seek(0)
    return result


def _archive_members(members, payload=None):
    result = io.BytesIO()
    payload = (
        payload
        if payload is not None
        else yaml.safe_dump(
            {"apiVersion": "v2", "name": "gpu-operator", "version": "1.0.0"}
        ).encode()
    )
    with tarfile.open(fileobj=result, mode="w:gz") as archive:
        for name in members:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    result.seek(0)
    return result


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        (
            "https://example.test/charts/index.yaml?token=secret#fragment",
            "https://example.test/charts/index.yaml?token=secret",
        ),
        (
            "https://example.test/charts/?token=secret",
            "https://example.test/charts/index.yaml?token=secret",
        ),
    ],
)
def test_index_url_retains_remote_query(remote, expected):
    assert repository_index_url(remote) == expected
    assert resolve_chart_url(remote, "sample-1.0.0.tgz") == (
        "https://example.test/charts/sample-1.0.0.tgz"
    )
    assert resolve_chart_url(remote, "sample-1.0.0.tgz?archive-token=own") == (
        "https://example.test/charts/sample-1.0.0.tgz?archive-token=own"
    )
    assert validate_chart_url(remote, "https://example.test/charts/sample.tgz") == (
        "https://example.test/charts/sample.tgz"
    )
    assert (
        validate_chart_url(
            remote, "https://github.com/example/sample.tgz?token=own", ["github.com"]
        )
        == "https://github.com/example/sample.tgz?token=own"
    )
    assert redact_url(expected) == "https://example.test/charts/index.yaml"
