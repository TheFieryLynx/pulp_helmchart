# Generated manually for pulp-helmchart remote sync filtering.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("helmchart", "0002_remote_and_sync"),
    ]

    operations = [
        migrations.AddField(
            model_name="helmchartremote",
            name="include_charts",
            field=models.JSONField(default=list),
        ),
        migrations.AddField(
            model_name="helmchartremote",
            name="include_versions",
            field=models.JSONField(default=list),
        ),
        migrations.AddField(
            model_name="helmchartremote",
            name="latest_only",
            field=models.BooleanField(default=False),
        ),
    ]
