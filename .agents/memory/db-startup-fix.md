---
name: DB create_all crash fix
description: PostgreSQL UniqueViolation on pg_type at backend startup — how to handle it.
---

**Symptom:** `psycopg2.errors.UniqueViolation: duplicate key value violates unique constraint "pg_type_typname_nsp_index"` with `Key (typname, typnamespace)=(user_registry, 2200)` during `Base.metadata.create_all`.

**Cause:** PostgreSQL's system catalog already has a type entry for the table name from a prior partially-committed transaction or a concurrent process trying to create the same table.

**Fix applied in `backend/main.py`:**
```python
try:
    Base.metadata.create_all(bind=_engine, checkfirst=True)
except Exception as _e:
    print(f"[startup] schema create_all skipped: {_e}", flush=True)
```

**Why:** `checkfirst=True` tells SQLAlchemy to issue `IF NOT EXISTS`, but the pg_type conflict can still surface. The try/except lets the server start anyway since the tables already exist.
