"""Single version constant for the Lorekeeper Hermes plugin (provider + client).

Mirrors the ``version`` field in plugin.yaml and the ``SERVICE_VERSION``
constant in server/index.js — bump all three together. Logged at startup:
the client logs its version on first request, the provider logs provider +
service versions (from /health) at initialize, the service logs its own at
boot. This is how we track which code is actually loaded on the box.
"""

__version__ = "0.2.7"