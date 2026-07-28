"""Block real network access when the test directory is on ``PYTHONPATH``."""

from __future__ import annotations

import socket
from typing import NoReturn


def _deny_network(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError(
        "real network access is disabled for the synthetic test suite"
    )


socket.create_connection = _deny_network
socket.getaddrinfo = _deny_network
