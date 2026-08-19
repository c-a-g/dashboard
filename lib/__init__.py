"""Internals for the notice tracker.

Only two things sit in the project root: `main.py`, the CLI you type, and
`profile.json`, the file you edit to tune scoring. Everything else lives here.

    store               schema, upserts, change diffing, retention, hidden list
    api_client          the upstream API client
    scoring             the fit-scoring engine, driven by ../profile.json
    serve               local web server -- what the Notices Dashboard launchers run
    dashboard           builds ../dashboard.html from dashboard_template.html

`serve` and `dashboard` are runnable, but you rarely run them: the launcher
starts `serve`, and a sync runs `dashboard` at the end. Both are executed by
path rather than imported, so each puts the project root on `sys.path` before
importing its siblings -- see the note at the top of `serve.py`.
"""
