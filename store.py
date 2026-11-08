import os
import csv
import io
import json
import sqlite3
import threading
import logging
from datetime import datetime, timezone


class Store:
    def __init__(self, data_dir="."):
        os.makedirs(data_dir, exist_ok=True)
        self.db_path = os.path.join(data_dir, "stats.db")
        self.map_path = os.path.join(data_dir, "emoji_map.json")
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS rolls (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, game TEXT,
            value INTEGER, message_id INTEGER, timestamp TEXT
        )""")
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(rolls)")]
        if "deleted" not in cols:
            self.conn.execute("ALTER TABLE rolls ADD COLUMN deleted INTEGER DEFAULT 0")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_rolls_ug ON rolls (user_id, game, id)")
        self.conn.commit()
        self.map = self.load_map()

    def load_map(self):
        if not os.path.exists(self.map_path):
            return {}
        try:
            with open(self.map_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError) as e:
            logging.error(f"emoji_map.json не прочитан: {e}")
            try:
                os.replace(self.map_path, self.map_path + ".bak")
            except OSError:
                pass
            return {}

    def save_map(self):
        tmp = self.map_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.map, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.map_path)

    def labels(self, game):
        return self.map.get(game, {})

    def set_label(self, game, value, name):
        self.map.setdefault(game, {})[str(value)] = name
        self.save_map()

    def reset_labels(self, game):
        self.map.pop(game, None)
        self.save_map()

    def add_rolls(self, user_id, game, rows):
        ts = datetime.now(timezone.utc).isoformat()
        data = [(user_id, game, v, m, ts) for v, m in rows]
        with self.lock:
            self.conn.executemany(
                "INSERT INTO rolls (user_id, game, value, message_id, timestamp) VALUES (?,?,?,?,?)", data)
            self.conn.commit()

    def values(self, user_id, game, limit=50000):
        with self.lock:
            cur = self.conn.execute(
                "SELECT value FROM (SELECT id, value FROM rolls WHERE user_id=? AND game=? ORDER BY id DESC LIMIT ?) ORDER BY id",
                (user_id, game, limit))
            return [r[0] for r in cur.fetchall()]

    def live_message_ids(self, user_id, limit=5000):
        with self.lock:
            cur = self.conn.execute(
                "SELECT message_id FROM rolls WHERE user_id=? AND deleted=0 ORDER BY id DESC LIMIT ?",
                (user_id, limit))
            return [r[0] for r in cur.fetchall()]

    def mark_deleted(self, user_id, ids):
        with self.lock:
            self.conn.executemany(
                "UPDATE rolls SET deleted=1 WHERE user_id=? AND message_id=?", [(user_id, i) for i in ids])
            self.conn.commit()

    def export_csv(self, user_id, game=None):
        query = "SELECT id, game, value, timestamp FROM rolls WHERE user_id=?"
        args = [user_id]
        if game:
            query += " AND game=?"
            args.append(game)
        query += " ORDER BY id"
        with self.lock:
            rows = self.conn.execute(query, args).fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "game", "value", "timestamp"])
        w.writerows(rows)
        return buf.getvalue().encode("utf-8"), len(rows)

    def close(self):
        with self.lock:
            self.conn.close()
