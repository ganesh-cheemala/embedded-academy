# Embedded Academy v17 — Starlette/Jinja Compatibility Fix

Render was resolving Starlette 1.0.0 on Python 3.14. That combination can
cause Jinja2 TemplateResponse to fail with:

`TypeError: cannot use 'tuple' as a dict key (unhashable type: 'dict')`

v17 pins Starlette to the pre-1.0 series while retaining FastAPI/Jinja2
support. No Render port or start-command changes are required.
