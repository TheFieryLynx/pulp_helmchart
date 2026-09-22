from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ModelState

from pulp_helmchart.app.models import HelmChartRemote
from pulp_helmchart.helm import RepositoryChartEntry, filter_repository_entries

migration = import_module("pulp_helmchart.app.migrations.0006_remote_sync_policies")


def test_sync_fields_match_migration_state():
    migrated = (
        MigrationLoader(None).project_state().models[("helmchart", "helmchartremote")]
    )
    current = ModelState.from_model(HelmChartRemote)
    for name in (
        "include_versions",
        "exclude_versions",
        "checksum_mismatch_policy",
        "auto_excluded_versions",
    ):
        assert migrated.fields[name].deconstruct() == current.fields[name].deconstruct()


@pytest.mark.parametrize(
    "includes, excludes, expected_includes, expected_excludes, selected",
    [
        ([], [], {}, {}, ["1.18.0", "1.17.0", "1.16.0"]),
        (
            ["1.18.0", "1.17.0"],
            [],
            {"*": ["1.18.0", "1.17.0"]},
            {},
            ["1.18.0", "1.17.0"],
        ),
        ([], ["1.16.0"], {}, {"*": ["1.16.0"]}, ["1.18.0", "1.17.0"]),
        (
            ["1.18.0", "1.17.0"],
            ["1.17.0"],
            {"*": ["1.18.0", "1.17.0"]},
            {"*": ["1.17.0"]},
            ["1.18.0"],
        ),
    ],
)
def test_migration_preserves_global_version_filters(
    includes, excludes, expected_includes, expected_excludes, selected
):
    remote = SimpleNamespace(
        include_versions=includes, exclude_versions=excludes, save=Mock()
    )
    model = Mock()
    model.objects.using.return_value.all.return_value.iterator.return_value = iter(
        [remote]
    )
    apps = Mock(get_model=Mock(return_value=model))
    editor = SimpleNamespace(connection=SimpleNamespace(alias="test-db"))

    migration.migrate_version_filters(apps, editor)

    assert remote.include_versions == expected_includes
    assert remote.exclude_versions == expected_excludes
    remote.save.assert_called_once_with(
        using="test-db", update_fields=["include_versions", "exclude_versions"]
    )
    entries = [
        RepositoryChartEntry(name, version, ["chart.tgz"], None, {})
        for name in ("alertmanager", "other-chart")
        for version in ("1.18.0", "1.17.0", "1.16.0")
    ]
    result = filter_repository_entries(
        entries,
        include_versions=remote.include_versions,
        exclude_versions=remote.exclude_versions,
    )
    assert [(entry.chart_name, entry.version) for entry in result] == [
        (name, version)
        for name in ("alertmanager", "other-chart")
        for version in selected
    ]


def test_migration_leaves_existing_mappings_untouched():
    remote = SimpleNamespace(
        include_versions={"chart": []}, exclude_versions={"*": ["1"]}, save=Mock()
    )
    model = Mock()
    model.objects.using.return_value.all.return_value.iterator.return_value = iter(
        [remote]
    )
    migration.migrate_version_filters(
        Mock(get_model=Mock(return_value=model)),
        SimpleNamespace(connection=SimpleNamespace(alias="default")),
    )
    remote.save.assert_not_called()
    assert remote.include_versions == {"chart": []}
    assert remote.exclude_versions == {"*": ["1"]}
