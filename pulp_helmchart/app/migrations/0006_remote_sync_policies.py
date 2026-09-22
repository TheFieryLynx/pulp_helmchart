from django.db import migrations, models


def migrate_version_filters(apps, schema_editor):
    """Preserve legacy global version lists as wildcard rules."""
    Remote = apps.get_model("helmchart", "HelmChartRemote")
    for remote in Remote.objects.using(schema_editor.connection.alias).all().iterator():
        changed = []
        for field in ("include_versions", "exclude_versions"):
            versions = getattr(remote, field)
            if isinstance(versions, dict):
                continue
            setattr(remote, field, {"*": versions} if versions else {})
            changed.append(field)
        if changed:
            remote.save(using=schema_editor.connection.alias, update_fields=changed)


class Migration(migrations.Migration):
    dependencies = [("helmchart", "0005_remote_exclude_versions")]

    operations = [
        migrations.RunPython(migrate_version_filters),
        migrations.AlterField(
            model_name="helmchartremote",
            name="include_versions",
            field=models.JSONField(default=dict),
        ),
        migrations.AlterField(
            model_name="helmchartremote",
            name="exclude_versions",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="helmchartremote",
            name="checksum_mismatch_policy",
            field=models.CharField(
                choices=[("fail", "fail"), ("skip", "skip"), ("exclude", "exclude")],
                default="fail",
                max_length=7,
            ),
        ),
        migrations.AddField(
            model_name="helmchartremote",
            name="auto_excluded_versions",
            field=models.JSONField(default=dict),
        ),
    ]
