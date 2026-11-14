import asyncio

from django.core.management.base import BaseCommand

from plane.coordination.codex.config import load_config
from plane.coordination.codex.runtime import Runtime


class Command(BaseCommand):
    help = "Connect Plane directly to configured Codex app-server WebSockets"

    def add_arguments(self, parser):
        parser.add_argument("--config", required=True, help="Path to the private agent-infrastructure JSON manifest")

    def handle(self, *args, **options):
        asyncio.run(Runtime(load_config(options["config"])).run())
