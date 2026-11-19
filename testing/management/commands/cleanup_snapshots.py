"""
Удаляет снимки прокторинга (записи и файлы) старше SNAPSHOT_RETENTION_DAYS (по умолчанию 30).

Запускается раз в сутки из entrypoint.sh; вручную:
    python manage.py cleanup_snapshots
"""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Удаляет снимки прокторинга старше SNAPSHOT_RETENTION_DAYS"

    def handle(self, *args, **options):
        from testing.models import AttemptSnapshot

        days = settings.SNAPSHOT_RETENTION_DAYS
        cutoff = timezone.now() - timezone.timedelta(days=days)
        # queryset.delete() шлёт post_delete на каждый объект — файлы удаляются сигналом
        deleted, _ = AttemptSnapshot.objects.filter(created_at__lt=cutoff).delete()
        self.stdout.write(self.style.SUCCESS(f"Удалено снимков старше {days} дней: {deleted}."))
