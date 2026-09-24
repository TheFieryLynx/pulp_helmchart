from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("helmchart", "0006_remote_sync_policies")]

    operations = [
        migrations.AddField(
            model_name="helmchartremote",
            name="allowed_chart_hosts",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
