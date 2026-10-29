"""
Grant, revoke or list the Historical Backfill operator role (a Django group).

    manage.py publisher_backfill_operator --list
    manage.py publisher_backfill_operator --grant <username>
    manage.py publisher_backfill_operator --revoke <username>

Membership is what lets a signed-in user START, CANCEL or RETRY a historical backfill in the Publishing
Monitor; previewing needs only a normal sign-in. Nothing else about the account changes (no staff or
superuser flag, no password), and an unknown username is refused rather than created.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Grant, revoke or list the Publishing Monitor's Historical Backfill operator role."

    def add_arguments(self, parser):
        action = parser.add_mutually_exclusive_group(required=True)
        action.add_argument("--grant", metavar="USERNAME")
        action.add_argument("--revoke", metavar="USERNAME")
        action.add_argument("--list", action="store_true")

    def handle(self, *args, **options):
        name = settings.PUBLISHER_BACKFILL_OPERATOR_GROUP
        if options["list"]:
            group = Group.objects.filter(name=name).first()
            members = sorted(group.user_set.values_list("username", flat=True)) if group else []
            self.stdout.write(f"{name}: {', '.join(members) if members else '(no operators)'}")
            return
        username = options["grant"] or options["revoke"]
        user = get_user_model().objects.filter(username=username).first()
        if user is None:
            raise CommandError(f"no user named {username!r}; the user must sign in once first")
        if options["grant"]:
            group, _ = Group.objects.get_or_create(name=name)
            user.groups.add(group)
            self.stdout.write(f"granted: {username} is a historical backfill operator")
        else:
            group = Group.objects.filter(name=name).first()
            if group is not None:
                user.groups.remove(group)
            self.stdout.write(f"revoked: {username} is not a historical backfill operator")
