from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0137_photo_ocr_source_dimensions"),
    ]

    operations = [
        migrations.AddField(
            model_name="photo",
            name="dhash",
            field=models.CharField(blank=True, db_index=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="photo",
            name="color_hash",
            field=models.CharField(blank=True, db_index=True, max_length=64, null=True),
        ),
    ]
