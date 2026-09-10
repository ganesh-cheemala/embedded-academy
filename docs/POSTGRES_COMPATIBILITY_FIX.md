# KCI Academy v16 — PostgreSQL Compatibility Fix

v16 fixes PostgreSQL dictionary-row compatibility for code paths that use
SQLite-style integer indexing such as `fetchone()[0]`.

The PostgreSQL row factory now supports both:
- `row["column_name"]`
- `row[0]`

No Render start-command or port changes are required for this fix.
