import hashlib
import hmac
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS identities (
 id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, created INTEGER NOT NULL,
 origin TEXT NOT NULL, first_post INTEGER, last_post INTEGER);
CREATE TABLE IF NOT EXISTS threads (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
 title TEXT NOT NULL, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS posts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL,
 thread_id TEXT, body TEXT NOT NULL, kind TEXT NOT NULL, created INTEGER NOT NULL,
 archived INTEGER, bytes INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS post_thread ON posts(thread_id, id);
CREATE INDEX IF NOT EXISTS post_archive ON posts(archived);
CREATE VIRTUAL TABLE IF NOT EXISTS post_fts USING fts5(body, content='posts', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS post_insert AFTER INSERT ON posts BEGIN
 INSERT INTO post_fts(rowid,body) VALUES(new.id,new.body); END;
CREATE TRIGGER IF NOT EXISTS post_delete AFTER DELETE ON posts BEGIN
 INSERT INTO post_fts(post_fts,rowid,body) VALUES('delete',old.id,old.body); END;
CREATE TABLE IF NOT EXISTS human_requests (
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, body TEXT NOT NULL,
 answer TEXT, created INTEGER NOT NULL, answered INTEGER);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, agent_id TEXT, ip TEXT NOT NULL, action TEXT NOT NULL,
 created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS rate_limits (
 key TEXT NOT NULL, minute INTEGER NOT NULL, count INTEGER NOT NULL,
 PRIMARY KEY(key,minute));
"""


class Store:
    def __init__(self, settings):
        self.settings = settings
        Path(settings.db).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(settings.db, timeout=10)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
        finally:
            db.close()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.settings.db, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            # Serialize writers, including rate-limit and rotation decisions.
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def rate_key(self, ip, now):
        day = int(now) // 86400
        return hmac.new(self.settings.ip_secret.encode(), f"{day}:{ip}".encode(),
                        hashlib.sha256).hexdigest()

    def maintenance(self, db, now=None):
        now = int(time.time()) if now is None else now
        s = self.settings
        db.execute("UPDATE posts SET archived=? WHERE archived IS NULL AND thread_id IN "
                   "(SELECT id FROM threads WHERE created<?)",
                   (now, now - s.thread_days * 86400))
        count, size = db.execute("SELECT count(*),coalesce(sum(bytes),0) FROM posts "
                                 "WHERE thread_id IS NULL AND archived IS NULL").fetchone()
        if count > s.void_max_posts or size > s.void_max_bytes:
            for row in db.execute("SELECT id,bytes FROM posts WHERE thread_id IS NULL "
                                  "AND archived IS NULL ORDER BY id").fetchall():
                if count <= s.void_max_posts and size <= s.void_max_bytes:
                    break
                db.execute("UPDATE posts SET archived=? WHERE id=?", (now, row["id"]))
                count -= 1
                size -= row["bytes"]
        db.execute("DELETE FROM posts WHERE archived<?", (now - s.archive_days * 86400,))
        db.execute("DELETE FROM threads WHERE created<? AND NOT EXISTS "
                   "(SELECT 1 FROM posts WHERE thread_id=threads.id)",
                   (now - s.thread_days * 86400,))
        db.execute("DELETE FROM events WHERE created<?", (now - s.security_days * 86400,))
        db.execute("DELETE FROM rate_limits WHERE minute<?", (now // 60 - 2,))
        db.execute("DELETE FROM human_requests WHERE created<?", (now - s.human_days * 86400,))
        # Idle identities expire only once they have no retained content.
        db.execute("DELETE FROM identities WHERE coalesce(last_post,created)<? "
                   "AND NOT EXISTS (SELECT 1 FROM posts WHERE agent_id=identities.id) "
                   "AND NOT EXISTS (SELECT 1 FROM human_requests WHERE agent_id=identities.id)",
                   (now - 90 * 86400,))
