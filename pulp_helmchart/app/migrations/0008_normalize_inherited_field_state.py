"""Align historical field deconstruction with inherited model defaults.

0001 explicitly serialized related_name for parent pointers and _pulp_domain.
The model's default_related_name gives the same runtime relation names, so these
operations change migration state only and must not alter existing tables.
"""

import django.db.models.deletion
import pulpcore.app.util
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("helmchart", "0007_allowed_chart_hosts")]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="helmchartcontent",
                    name="_pulp_domain",
                    field=models.ForeignKey(
                        default=pulpcore.app.util.get_domain_pk,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="core.domain",
                    ),
                ),
                migrations.AlterField(
                    model_name="helmchartcontent",
                    name="content_ptr",
                    field=models.OneToOneField(
                        auto_created=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        parent_link=True,
                        primary_key=True,
                        serialize=False,
                        to="core.content",
                    ),
                ),
                migrations.AlterField(
                    model_name="helmchartdistribution",
                    name="distribution_ptr",
                    field=models.OneToOneField(
                        auto_created=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        parent_link=True,
                        primary_key=True,
                        serialize=False,
                        to="core.distribution",
                    ),
                ),
                migrations.AlterField(
                    model_name="helmchartpublication",
                    name="publication_ptr",
                    field=models.OneToOneField(
                        auto_created=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        parent_link=True,
                        primary_key=True,
                        serialize=False,
                        to="core.publication",
                    ),
                ),
                migrations.AlterField(
                    model_name="helmchartremote",
                    name="remote_ptr",
                    field=models.OneToOneField(
                        auto_created=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        parent_link=True,
                        primary_key=True,
                        serialize=False,
                        to="core.remote",
                    ),
                ),
                migrations.AlterField(
                    model_name="helmchartrepository",
                    name="repository_ptr",
                    field=models.OneToOneField(
                        auto_created=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        parent_link=True,
                        primary_key=True,
                        serialize=False,
                        to="core.repository",
                    ),
                ),
            ],
            database_operations=[],
        )
    ]
