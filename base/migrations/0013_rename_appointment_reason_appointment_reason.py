# Generated by Django 4.0.3 on 2022-03-23 10:00

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0012_appointment_doctors_alter_appointment_clients'),
    ]

    operations = [
        migrations.RenameField(
            model_name='appointment',
            old_name='appointment_reason',
            new_name='reason',
        ),
    ]