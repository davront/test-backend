# Куратор тестов: без прав на группы (через change_group можно было выдать группе любые
# права) и с правами на группы вопросов (иначе вопросы в админке ему не видны).

from django.apps import apps as django_apps
from django.contrib.auth.management import create_permissions
from django.db import migrations

TEST_CURATOR_GROUP = "Куратор тестов"

_REVOKE = (("auth", "add_group"), ("auth", "change_group"), ("auth", "delete_group"))
_GRANT = tuple(
    ("testing", f"{action}_questiongroup")
    for action in ("add", "change", "delete", "view")
)


def _perms(Permission, pairs):
    perms = []
    for app_label, codename in pairs:
        perms.extend(
            Permission.objects.filter(content_type__app_label=app_label, codename=codename)
        )
    return perms


def forwards(apps, schema_editor):
    # Права создаются в post_migrate, т.е. на чистой БД их ещё нет — создаём явно (как в 0013).
    using = schema_editor.connection.alias
    for label in ("auth", "testing"):
        create_permissions(
            django_apps.get_app_config(label),
            verbosity=0,
            interactive=False,
            using=using,
            apps=django_apps,
        )
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    group = Group.objects.filter(name=TEST_CURATOR_GROUP).first()
    if not group:
        return
    group.permissions.remove(*_perms(Permission, _REVOKE))
    group.permissions.add(*_perms(Permission, _GRANT))


def backwards(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    group = Group.objects.filter(name=TEST_CURATOR_GROUP).first()
    if not group:
        return
    group.permissions.remove(*_perms(Permission, _GRANT))
    group.permissions.add(*_perms(Permission, _REVOKE[:2]))


class Migration(migrations.Migration):

    dependencies = [
        ("testing", "0021_attemptresponse_protect"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
