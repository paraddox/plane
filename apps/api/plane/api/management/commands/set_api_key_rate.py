# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from plane.api.rate_limit import APIKeyRateLimitUnavailable, get_api_key_rate
from plane.db.models import APIToken


class Command(BaseCommand):
    help = "Set an operator-controlled per-key rate, or restore the inherited rate policy."

    def add_arguments(self, parser):
        parser.add_argument("token_id", type=UUID)
        policy = parser.add_mutually_exclusive_group(required=True)
        policy.add_argument("--rate", help="A positive request count and period, such as COUNT/minute.")
        policy.add_argument("--inherit", action="store_true", help="Use the deployment or service-token default.")

    def handle(self, *args, **options):
        try:
            token = APIToken.objects.get(pk=options["token_id"])
        except APIToken.DoesNotExist:
            raise CommandError("API key not found.") from None

        token.allowed_rate_limit = "" if options["inherit"] else options["rate"]
        if not options["inherit"] and not token.allowed_rate_limit:
            raise CommandError("The rate must not be empty; use --inherit to restore the default.")
        try:
            count, duration = get_api_key_rate(token)
        except APIKeyRateLimitUnavailable:
            raise CommandError("The rate must contain a positive count and a second/minute/hour/day period.") from None

        token.save(update_fields=["allowed_rate_limit"])
        self.stdout.write(f"API key {token.pk}: {count} requests per {duration} seconds.")
