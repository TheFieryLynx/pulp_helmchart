# Generated manually for pulp-helmchart unavailable chart handling.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("helmchart", "0003_remote_filters"),
    ]

    operations = [
        migrations.AddField(
            model_name="helmchartremote",
            name="exclude_charts",
            field=models.JSONField(default=list),
        ),
        migrations.AddField(
            model_name="helmchartremote",
            name="ignore_unavailable",
            field=models.BooleanField(default=True),
        ),
    ]
