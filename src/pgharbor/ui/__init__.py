"""The management panel: a local web page for the owner of a pgharbor installation.

It is a separate program (``pgharbor ui``), started by a person and only while that person
wants it. The MCP server never starts it, never imports it and never listens on a port:
what an agent talks to and what the owner manages stay two different processes.

The panel reads what the servers record (the audit log, the session files) and edits the
one file that holds the rules (the configuration). Its own safety model is in
``docs/security.md``, section "The management panel", and in ``gate.py``.
"""

DEFAULT_PORT = 7432
