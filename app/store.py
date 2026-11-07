import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class Store:
    def __init__(self, path):
        self.path = str(path)
        self._keeper = None
        if self.path == ":memory:":
            self.path = f"file:account_watch_{uuid4().hex}?mode=memory&cache=shared"
            self._keeper = self._connect()
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, data TEXT, PRIMARY KEY(kind,id))")

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30, uri=self.path.startswith("file:"))
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def transaction(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get(self, kind, id, *, conn=None):
        if conn is None:
            with self.transaction() as conn:
                return self.get(kind, id, conn=conn)
        row = conn.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, id)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, kind, *, conn=None):
        if conn is None:
            with self.transaction() as conn:
                return self.list(kind, conn=conn)
        return [json.loads(row[0]) for row in conn.execute("SELECT data FROM records WHERE kind=? ORDER BY rowid", (kind,))]

    def put(self, kind, record, *, conn=None):
        if not isinstance(record.get("id"), str) or not record["id"]:
            raise ValueError("Records require a nonempty string id")
        if conn is None:
            with self.transaction() as conn:
                return self.put(kind, record, conn=conn)
        conn.execute("INSERT INTO records(kind,id,data) VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data", (kind, record["id"], json.dumps(record, allow_nan=False)))
        return record

    def delete(self, kind, id, *, conn=None):
        if conn is None:
            with self.transaction() as conn:
                return self.delete(kind, id, conn=conn)
        conn.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, id))
