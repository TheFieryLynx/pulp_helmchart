# Generated manually for pulp-helmchart initial schema.

from django.db import migrations, models
import django.db.models.deletion
import pulpcore.app.util


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0102_add_domain_relations"),
    ]

    operations = [
        migrations.CreateModel(
            name="HelmChartContent",
            fields=[
                (
                    "content_ptr",
                    models.OneToOneField(
                        auto_created=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        parent_link=True,
                        primary_key=True,
                        related_name="helmchart_helmchartcontent",
                        serialize=False,
                        to="core.content",
                    ),
                ),
                ("name", models.TextField()),
                ("version", models.TextField()),
                ("api_version", models.TextField(null=True)),
                ("app_version", models.TextField(null=True)),
                ("description", models.TextField(null=True)),
                ("digest", models.CharField(max_length=64)),
                ("filename", models.TextField()),
                ("chart_yaml", models.JSONField(default=dict)),
                (
                    "_pulp_domain",
                    models.ForeignKey(
                        default=pulpcore.app.util.get_domain_pk,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="helmchart_helmchartcontent",
                        to="core.domain",
                    ),
                ),
            ],
            options={
                "default_related_name": "%(app_label)s_%(model_name)s",
                "permissions": [
                    ("upload_helmchart", "Can upload Helm chart content using synchronous API."),
                ],
                "unique_together": {("name", "version", "digest", "_pulp_domain")},
            },
            bases=("core.content",),
        ),
        migrations.CreateModel(
            name="HelmChartRepository",
            fields=[
                (
                    "repository_ptr",
                    models.OneToOneField(
                        auto_created=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        parent_link=True,
                        primary_key=True,
                        related_name="helmchart_helmchartrepository",
                        serialize=False,
                        to="core.repository",
                    ),
                ),
                ("autopublish", models.BooleanField(default=False)),
                ("last_sync_details", models.JSONField(default=dict)),
            ],
            options={
                "default_related_name": "%(app_label)s_%(model_name)s",
                "permissions": [
                    (
                        "modify_helmchartrepository",
                        "Can modify content of the Helm chart repository",
                    ),
                    (
                        "manage_roles_helmchartrepository",
                        "Can manage roles on Helm chart repositories",
                    ),
                    ("repair_helmchartrepository", "Can repair repository versions"),
                ],
            },
            bases=("core.repository",),
        ),
        migrations.CreateModel(
            name="HelmChartPublication",
            fields=[
                (
                    "publication_ptr",
                    models.OneToOneField(
                        auto_created=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        parent_link=True,
                        primary_key=True,
                        related_name="helmchart_helmchartpublication",
                        serialize=False,
                        to="core.publication",
                    ),
                ),
                ("index", models.TextField(default="index.yaml")),
            ],
            options={
                "abstract": False,
                "default_related_name": "%(app_label)s_%(model_name)s",
                "permissions": [
                    (
                        "manage_roles_helmchartpublication",
                        "Can manage roles on Helm chart publications",
                    ),
                ],
            },
            bases=("core.publication",),
        ),
        migrations.CreateModel(
            name="HelmChartDistribution",
            fields=[
                (
                    "distribution_ptr",
                    models.OneToOneField(
                        auto_created=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        parent_link=True,
                        primary_key=True,
                        related_name="helmchart_helmchartdistribution",
                        serialize=False,
                        to="core.distribution",
                    ),
                ),
            ],
            options={
                "default_related_name": "%(app_label)s_%(model_name)s",
                "permissions": [
                    (
                        "manage_roles_helmchartdistribution",
                        "Can manage roles on Helm chart distributions",
                    ),
                ],
            },
            bases=("core.distribution",),
        ),
    ]
