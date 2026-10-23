"""Synchronous entry point for collection before a background AI analysis."""

from __future__ import annotations

import asyncio
from pathlib import Path


COLLECTION_TIMEOUT = 600


def collect_news(directory, timeout=COLLECTION_TIMEOUT):
    # Import browser/HTML dependencies only when collection is requested.
    from .__main__ import collect_to_directory
    from .models import Settings

    directory = Path(directory)
    settings = Settings(browser_state=str(directory / ".browser-state.json"))

    async def collect():
        # Cancellation closes Transport and releases Storage's lock before an
        # error reaches the job. No analysis starts on an unfinished batch.
        return await asyncio.wait_for(
            collect_to_directory(settings, directory), timeout=timeout,
        )

    return asyncio.run(collect())
