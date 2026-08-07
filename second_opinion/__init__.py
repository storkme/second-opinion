"""Independent, agentic second-opinion PR reviewer (OpenRouter or a local llama-server)."""

from importlib.metadata import PackageNotFoundError, version as _version

# Derived, never written down twice. A literal here read "0.1.0" for eight releases
# because nothing imports it, so nothing caught the drift — and the release procedure
# (CLAUDE.md) deliberately keeps only three things in agreement: pyproject's version,
# the newest CHANGELOG heading, and the tag. Reading the installed metadata keeps it
# at three rather than adding a fourth that silently rots.
#
# Caveat, should anything ever start reading this: the Action image does NOT install
# the package — the Dockerfile copies second_opinion/ in and runs it off PYTHONPATH —
# so there is no distribution to read metadata from and this is "unknown" in the
# Action, the primary delivery. Honest, but not useful: stamping a version into an
# event or a comment footer needs a build-time value copied into the image, not this.
try:
    __version__ = _version("second-opinion")
except PackageNotFoundError:  # a source tree (or the Action image) with no install
    __version__ = "unknown"
