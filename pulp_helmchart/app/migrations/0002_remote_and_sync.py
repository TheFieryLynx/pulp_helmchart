# Generated manually for pulp-helmchart remote sync support.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0102_add_domain_relations"),
        ("helmchart", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="HelmChartRemote",
            fields=[
                (
                    "remote_ptr",
                    models.OneToOneField(
                        auto_created=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        parent_link=True,
                        primary_key=True,
                        related_name="helmchart_helmchartremote",
                        serialize=False,
                        to="core.remote",
                    ),
                ),
            ],
            options={
                "default_related_name": "%(app_label)s_%(model_name)s",
                "permissions": [
                    (
                        "manage_roles_helmchartremote",
                        "Can manage roles on Helm chart remotes",
                    ),
                ],
            },
            bases=("core.remote",),
        ),
        migrations.AlterModelOptions(
            name="helmchartrepository",
            options={
                "default_related_name": "%(app_label)s_%(model_name)s",
                "permissions": [
                    (
                        "modify_helmchartrepository",
                        "Can modify content of the Helm chart repository",
                    ),
                    ("sync_helmchartrepository", "Can sync the Helm chart repository"),
                    (
                        "manage_roles_helmchartrepository",
                        "Can manage roles on Helm chart repositories",
                    ),
                    ("repair_helmchartrepository", "Can repair repository versions"),
                ],
            },
        ),
    ]
