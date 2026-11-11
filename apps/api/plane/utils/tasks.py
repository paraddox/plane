# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from functools import partial

from django.db import transaction


def delay_on_commit(task, *args, **kwargs):
    """Publish after a successful database commit, or immediately in autocommit."""
    transaction.on_commit(partial(task.delay, *args, **kwargs))
