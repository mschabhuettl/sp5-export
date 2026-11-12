# sp5lib.orm

SQLAlchemy 2.0 layer (SQLite/PostgreSQL): 19 canonical models mirroring the
DBF tables, repositories, and the DBF → ORM sync (`sync.sync_all()`).
Purely additive — the DBF layer in `database.py` stays the source of truth.
Design, module map and known gaps: see the library's documentation.
