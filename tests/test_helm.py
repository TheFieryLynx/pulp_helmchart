import io
import tarfile

import pytest
import yaml

from pulp_helmchart.helm import (
    HelmChartError,
    filter_repository_entries,
    index_from_entries,
    parse_chart_archive,
    parse_repository_index,
    repository_index_url,
    resolve_chart_url,
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
    assert [entry["version"] for entry in parsed["entries"]["alpha"]] == ["2.0.0", "1.0.0"]
    assert parsed["entries"]["alpha"][0]["urls"] == ["alpha-2.0.0.tgz"]
    assert parsed["generated"] == "2026-01-02T00:00:00.000000Z"


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


def test_repository_index_and_chart_urls_are_resolved():
    remote = "https://helm.ngc.nvidia.com/nvidia"

    assert repository_index_url(remote) == "https://helm.ngc.nvidia.com/nvidia/index.yaml"
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
        verify_sha256_digest("sha256:expected", "actual", "https://example.test/chart.tgz")


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


def test_filter_repository_entries_latest_only_keeps_first_selected_entry():
    entries = _repository_entries()

    selected = filter_repository_entries(entries, include_charts=["gpu-operator"], latest_only=True)

    assert [(entry.chart_name, entry.version) for entry in selected] == [
        ("gpu-operator", "v26.3.3"),
    ]


def test_filter_repository_entries_excludes_versions_after_include():
    entries = _repository_entries()

    selected = filter_repository_entries(
        entries,
        include_charts=["gpu-operator"],
        include_versions=["v26.3.3", "v26.3.2"],
        exclude_versions=["v26.3.2"],
    )

    assert [(entry.chart_name, entry.version) for entry in selected] == [
        ("gpu-operator", "v26.3.3"),
    ]


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
