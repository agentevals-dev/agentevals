"""Postgres backend (asyncpg, no ORM).

Hand-written SQL because we lean on PG-specific features (FOR UPDATE SKIP
LOCKED, pg_try_advisory_lock, JSONB, ARRAY) that an ORM would obscure.
"""
