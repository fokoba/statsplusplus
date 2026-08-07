"""Database query modules for the web layer.

All query functions:
- Receive a connection as parameter (from web.context.get_conn())
- Return typed results (dicts with documented keys, or dataclass instances)
- Are read-only (no writes, no commits)
- Do not open their own connections

This eliminates the legacy pattern where each query function opened its own
connection, read state.json, and queried eval_date independently.
"""
