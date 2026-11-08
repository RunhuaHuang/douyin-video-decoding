"""Bounded metadata resolution for the portable Excel batch entry point."""
from functools import partial
import json
from pathlib import Path
import tempfile
import time

from .library_downloader import MetadataDownloader
from .library_resolver import IsolatedMetadataResolver


class TableResolver:
    def __init__(self, state_file, *, proxy=None, anonymous=False, reset_seconds=1800,
                 timeout_seconds=90):
        self.source = Path(state_file)
        self.anonymous = anonymous
        self.reset_seconds = reset_seconds
        self.timeout_seconds = timeout_seconds
        self.factory = partial(MetadataDownloader, proxy=proxy)
        self.engine = None
        self.temporary = None
        self.last_reset = time.monotonic()

    def resolve(self, url):
        if self.anonymous and time.monotonic() - self.last_reset >= self.reset_seconds:
            self.close()
        if self.engine is None:
            state = self.source
            if self.anonymous:
                # Each slot owns its disposable state. Never overwrite a user's
                # login/verification session or the account pool.
                self.temporary = tempfile.TemporaryDirectory(prefix='douyin-guest-')
                state = Path(self.temporary.name) / 'state.json'
                state.write_text(json.dumps({'cookies': [], 'origins': []}), encoding='utf-8')
            self.engine = IsolatedMetadataResolver(state, timeout_seconds=self.timeout_seconds,
                                                   factory=self.factory)
            self.last_reset = time.monotonic()
        return self.engine.resolve(url)

    def close(self):
        try:
            if self.engine is not None:
                self.engine.close()
        finally:
            self.engine = None
            if self.temporary is not None:
                self.temporary.cleanup()
                self.temporary = None
