from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0138_photo_archiver_hashes"),
    ]

    operations = [
        migrations.AlterField(
            model_name="photo",
            name="color_hash",
            field=models.CharField(blank=True, db_index=True, max_length=128, null=True),
        ),
    ]