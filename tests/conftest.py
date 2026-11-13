"""Global test configuration."""

import os

# Enable dev mode for API auth middleware during tests
os.environ.setdefault("SYMBIOTE_DEV_MODE", "1")

# Shared fixtures (see tests/support). Imported after the env var above so
# the dev-mode default is in place before any symbiote module is loaded.
from tests.support import api_client  # noqa: E402,F401
