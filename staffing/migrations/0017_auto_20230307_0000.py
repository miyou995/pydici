# Generated by Django 3.2.18 on 2023-03-07 00:00

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('staffing', '0016_auto_20230303_1646'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='staffing',
            options={'ordering': ['staffing_date', 'consultant', '-mission__nature', 'mission__lead__client__organisation__company__name', 'mission__description', 'id'], 'verbose_name': 'Staffing'},
        ),
        migrations.AlterField(
            model_name='staffing',
            name='staffing_date',
            field=models.DateField(db_index=True, verbose_name='Date'),
        ),
    ]