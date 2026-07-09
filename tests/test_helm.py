import io
import tarfile

import pytest
import yaml

from pulp_helmchart.helm import HelmChartError, index_from_entries, parse_chart_archive


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


def _chart_archive(chart_yaml):
    result = io.BytesIO()
    payload = yaml.safe_dump(chart_yaml).encode()
    with tarfile.open(fileobj=result, mode="w:gz") as archive:
        info = tarfile.TarInfo("gpu-operator/Chart.yaml")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    result.seek(0)
    return result
