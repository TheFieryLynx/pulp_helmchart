# Generated manually for pulp-helmchart remote version exclusion.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("helmchart", "0004_remote_unavailable_controls"),
    ]

    operations = [
        migrations.AddField(
            model_name="helmchartremote",
            name="exclude_versions",
            field=models.JSONField(default=list),
        ),
    ]
