"""Configuration for the Vrbo Scraper. Everything can be set with an
environment variable; the defaults work out of the box.

    PORT        port the API listens on (default 8000)
    VRBO_PROXY  proxy URL for every request, e.g. http://user:pass@host:port
                (default: none — direct). vrbo.com answers most home and
                office connections; if one starts answering 429 to every
                call, its IP has been flagged — point VRBO_PROXY at a US
                residential proxy and requests leave through it.

Everything else below is a plain constant with a working default — edit it
here if you need to.
"""
import os

PORT = int(os.environ.get("PORT", "8000"))

# Retry policy for transport errors (every request).
MAX_RETRIES = 3
RETRY_BACKOFF = 2          # seconds, multiplied by the attempt number

# A blocked request (vrbo's 429 "Bot or Not?" page) is retried this many
# times on a fresh session, alternating the two browser fingerprints the
# site accepts (vrbo/fetch.py IMPERSONATE_TARGETS).
VRBO_EXIT_ATTEMPTS = 4

# The hosted service moves to a pool of residential exits for a while after
# both fingerprints are refused; this kit has one connection, so it stays on
# it (0 = no fallback window).
VRBO_BLOCK_COOLDOWN = 0
VRBO_EXIT = "direct"

VRBO_PROXY = os.environ.get("VRBO_PROXY") or None


def vrbo_proxy():
    """The proxy for one curl session (None = direct)."""
    return os.environ.get("VRBO_PROXY") or None


def vrbo_fallback_proxy():
    """The exit used after both fingerprints are refused (None = keep the
    same connection)."""
    return os.environ.get("VRBO_PROXY") or None
