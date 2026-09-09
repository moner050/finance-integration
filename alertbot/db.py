"""MySQL 저장소 — 워치리스트, 엔진 상태, 신호 이력.

엔진 워커와 백오피스가 사용자의 기존 MySQL(.env 의 MYSQL_*)을 공유한다. 같은 데이터베이스에
다른 프로젝트의 테이블이 있으므로 이름은 alert_ 접두어를 쓴다. 테스트는 같은 함수로
메모리 SQLite 를 쓴다 — 자리표시자(%s→?)와 upsert 문만 방언이 다르다.

    python -m alertbot.db init     테이블 생성
    python -m alertbot.db seed     config.SEED_WATCHLIST 의 종목을 넣는다 (이미 있으면 건너뜀)
"""

import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone

from .config import MYSQL

log = logging.getLogger("scalper")

SCHEMA = {
    "mysql": [
        """CREATE TABLE IF NOT EXISTS alert_watchlist (
             symbol     VARCHAR(32) PRIMARY KEY,
             market     VARCHAR(2)  NOT NULL,
             name       VARCHAR(100),
             leaders    TEXT        NOT NULL,
             inverse    TINYINT     NOT NULL DEFAULT 0,
             pair       VARCHAR(32),
             hold_only  TINYINT     NOT NULL DEFAULT 0,
             note       VARCHAR(255),
             enabled    TINYINT     NOT NULL DEFAULT 1,
             created_at VARCHAR(32) NOT NULL,
             updated_at VARCHAR(32) NOT NULL
           ) CHARACTER SET utf8mb4""",
        """CREATE TABLE IF NOT EXISTS alert_engine_status (
             id           TINYINT PRIMARY KEY,
             heartbeat_at VARCHAR(32),
             active       TEXT NOT NULL,
             pre          TEXT NOT NULL,
             last_error   TEXT,
             state        MEDIUMTEXT NOT NULL,
             snapshots    MEDIUMTEXT NOT NULL
           ) CHARACTER SET utf8mb4""",
        """CREATE TABLE IF NOT EXISTS alert_signal_log (
             id        BIGINT AUTO_INCREMENT PRIMARY KEY,
             sent_at   VARCHAR(32)  NOT NULL,
             kind      VARCHAR(20)  NOT NULL,
             severity  VARCHAR(10)  NOT NULL,
             symbol    VARCHAR(32),
             label     VARCHAR(100),
             title     VARCHAR(100) NOT NULL,
             body      TEXT         NOT NULL,
             results   TEXT         NOT NULL,
             INDEX idx_alert_signal_sent (sent_at)
           ) CHARACTER SET utf8mb4""",
    ],
    "sqlite": [
        """CREATE TABLE IF NOT EXISTS alert_watchlist (
             symbol TEXT PRIMARY KEY, market TEXT NOT NULL, name TEXT, leaders TEXT NOT NULL,
             inverse INTEGER NOT NULL DEFAULT 0, pair TEXT, hold_only INTEGER NOT NULL DEFAULT 0,
             note TEXT, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS alert_engine_status (
             id INTEGER PRIMARY KEY, heartbeat_at TEXT, active TEXT NOT NULL, pre TEXT NOT NULL,
             last_error TEXT, state TEXT NOT NULL, snapshots TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS alert_signal_log (
             id INTEGER PRIMARY KEY AUTOINCREMENT, sent_at TEXT NOT NULL, kind TEXT NOT NULL,
             severity TEXT NOT NULL, symbol TEXT, label TEXT, title TEXT NOT NULL, body TEXT NOT NULL,
             results TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_alert_signal_sent ON alert_signal_log (sent_at)",
    ],
}

# upsert 는 방언이 다르다. MySQL 은 8.0.19+ 의 행 별칭(AS new) 구문 — VALUES() 는 8.0.20 부터 폐기 예정.
UPSERT_WATCH = {
    "mysql": """INSERT INTO alert_watchlist
                  (symbol, market, name, leaders, inverse, pair, hold_only, note, enabled, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) AS new
                ON DUPLICATE KEY UPDATE market=new.market, name=new.name, leaders=new.leaders,
                  inverse=new.inverse, pair=new.pair, hold_only=new.hold_only, note=new.note,
                  enabled=new.enabled, updated_at=new.updated_at""",
    "sqlite": """INSERT INTO alert_watchlist
                  (symbol, market, name, leaders, inverse, pair, hold_only, note, enabled, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(symbol) DO UPDATE SET market=excluded.market, name=excluded.name,
                  leaders=excluded.leaders, inverse=excluded.inverse, pair=excluded.pair,
                  hold_only=excluded.hold_only, note=excluded.note, enabled=excluded.enabled,
                  updated_at=excluded.updated_at""",
}
UPSERT_STATUS = {
    "mysql": """INSERT INTO alert_engine_status (id, heartbeat_at, active, pre, last_error, state, snapshots)
                VALUES (1, %s, %s, %s, %s, %s, %s) AS new
                ON DUPLICATE KEY UPDATE heartbeat_at=new.heartbeat_at, active=new.active, pre=new.pre,
                  last_error=new.last_error, state=new.state, snapshots=new.snapshots""",
    "sqlite": """INSERT INTO alert_engine_status (id, heartbeat_at, active, pre, last_error, state, snapshots)
                VALUES (1, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(id) DO UPDATE SET heartbeat_at=excluded.heartbeat_at, active=excluded.active,
                  pre=excluded.pre, last_error=excluded.last_error, state=excluded.state,
                  snapshots=excluded.snapshots""",
}


def _now() -> str:
    """마이크로초까지. 같은 초 안의 연속 수정도 watchlist_version 이 구분해야 핫리로드가 빠뜨리지 않는다."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class DB:
    """드라이버 차이를 감춘 얇은 래퍼. SQL 은 %s 자리표시자로 쓰고 SQLite 에선 ? 로 바꾼다.

    MySQL 연결은 오래 놀면 서버가 끊는다(wait_timeout, 오류 2006/2013). 실행이 그 오류로
    실패하면 새 연결을 맺어 한 번 더 시도한다.
    """

    def __init__(self, con, dialect: str, cfg: dict = None):
        self.con, self.dialect, self.cfg = con, dialect, cfg

    @staticmethod
    def _mysql_connect(cfg: dict):
        import pymysql
        return pymysql.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"],
                               password=cfg["password"], database=cfg["database"],
                               charset="utf8mb4", autocommit=True, connect_timeout=10,
                               cursorclass=pymysql.cursors.DictCursor)

    @classmethod
    def mysql(cls, cfg: dict = None) -> "DB":
        cfg = cfg or MYSQL
        return cls(cls._mysql_connect(cfg), "mysql", cfg)

    @classmethod
    def sqlite(cls, path: str = ":memory:") -> "DB":
        con = sqlite3.connect(path, isolation_level=None)      # autocommit
        con.row_factory = sqlite3.Row
        return cls(con, "sqlite")

    def execute(self, sql: str, params=()):
        if self.dialect == "sqlite":
            cur = self.con.cursor()
            cur.execute(sql.replace("%s", "?"), params)
            return cur
        import pymysql
        try:
            cur = self.con.cursor()
            cur.execute(sql, params)
            return cur
        except (pymysql.err.OperationalError, pymysql.err.InterfaceError) as e:
            log.warning("MySQL 연결 재수립 후 재시도: %s", e)
            try:
                self.con.close()
            except Exception:
                pass
            self.con = self._mysql_connect(self.cfg)
            cur = self.con.cursor()
            cur.execute(sql, params)
            return cur

    def fetchall(self, sql: str, params=()) -> list:
        return [dict(r) for r in self.execute(sql, params).fetchall()]

    def fetchone(self, sql: str, params=()):
        row = self.execute(sql, params).fetchone()
        return dict(row) if row else None

    def init_schema(self):
        for stmt in SCHEMA[self.dialect]:
            self.execute(stmt)
        return self

    def close(self):
        self.con.close()


def connect() -> DB:
    """운영용: MySQL 연결 + 테이블 보장."""
    return DB.mysql().init_schema()


# -- 워치리스트 ----------------------------------------------------------------

def _row_to_item(row: dict) -> dict:
    """행 → 엔진이 쓰는 설정 dict (원본 WATCHLIST 항목과 같은 모양)."""
    leaders = json.loads(row["leaders"] or "[]")
    return {
        "market": row["market"],
        "leaders": leaders or None,
        "inverse": bool(row["inverse"]),
        "pair": row["pair"] or None,
        "hold_only": bool(row["hold_only"]),
        "name": row["name"] or None,
        "note": row["note"] or None,
    }


def load_watchlist(db: DB, enabled_only: bool = True) -> dict:
    sql = "SELECT * FROM alert_watchlist" + (" WHERE enabled = 1" if enabled_only else "") + " ORDER BY market, symbol"
    return {row["symbol"]: _row_to_item(row) for row in db.fetchall(sql)}


def list_watch_rows(db: DB) -> list:
    """백오피스 표시용. enabled·시각 포함, leaders 는 리스트로."""
    rows = db.fetchall("SELECT * FROM alert_watchlist ORDER BY market, symbol")
    for r in rows:
        r["leaders"] = json.loads(r["leaders"] or "[]")
    return rows


def get_watch_row(db: DB, symbol: str):
    row = db.fetchone("SELECT * FROM alert_watchlist WHERE symbol = %s", (symbol,))
    if row:
        row["leaders"] = json.loads(row["leaders"] or "[]")
    return row


def watchlist_version(db: DB) -> str:
    """바뀌었는지만 알면 된다. 최종 수정 시각과 행 수를 합친 문자열 (삭제는 행 수로 잡힌다)."""
    row = db.fetchone("SELECT COALESCE(MAX(updated_at), '') AS u, COUNT(*) AS n FROM alert_watchlist")
    return f"{row['u']}:{row['n']}"


def upsert_watch(db: DB, symbol: str, market: str, name: str = None, leaders: list = None,
                 inverse: bool = False, pair: str = None, hold_only: bool = False,
                 note: str = None, enabled: bool = True):
    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError("symbol 이 비어 있다")
    if market not in ("US", "KR"):
        raise ValueError("market 은 US 또는 KR")
    now = _now()
    leaders_json = json.dumps([s.strip().upper() for s in (leaders or []) if s.strip()])
    db.execute(UPSERT_WATCH[db.dialect],
               (symbol, market, name or None, leaders_json, int(bool(inverse)),
                (pair or "").strip().upper() or None, int(bool(hold_only)), note or None,
                int(bool(enabled)), now, now))


def set_enabled(db: DB, symbol: str, enabled: bool):
    db.execute("UPDATE alert_watchlist SET enabled = %s, updated_at = %s WHERE symbol = %s",
               (int(bool(enabled)), _now(), symbol))


def delete_watch(db: DB, symbol: str):
    db.execute("DELETE FROM alert_watchlist WHERE symbol = %s", (symbol,))


def seed_watchlist(db: DB, items: dict) -> int:
    """없는 종목만 넣는다. 넣은 수를 돌려준다."""
    existing = {r["symbol"] for r in db.fetchall("SELECT symbol FROM alert_watchlist")}
    added = 0
    for symbol, cfg in items.items():
        if symbol in existing:
            continue
        upsert_watch(db, symbol, cfg["market"], cfg.get("name"), cfg.get("leaders") or [],
                     cfg.get("inverse", False), cfg.get("pair"), cfg.get("hold_only", False), cfg.get("note"))
        added += 1
    return added


# -- 엔진 상태 -----------------------------------------------------------------

def save_engine_status(db: DB, active: list, pre: list, state: dict, snapshots: dict, last_error: str = None):
    db.execute(UPSERT_STATUS[db.dialect],
               (_now(), json.dumps(active), json.dumps(pre), last_error,
                json.dumps(state, ensure_ascii=False), json.dumps(snapshots, ensure_ascii=False)))


def load_engine_status(db: DB):
    row = db.fetchone("SELECT * FROM alert_engine_status WHERE id = 1")
    if row is None:
        return None
    return {"heartbeat_at": row["heartbeat_at"], "active": json.loads(row["active"]),
            "pre": json.loads(row["pre"]), "last_error": row["last_error"],
            "state": json.loads(row["state"]), "snapshots": json.loads(row["snapshots"])}


# -- 신호 이력 -----------------------------------------------------------------

def log_signal(db: DB, signal, results: dict):
    db.execute(
        "INSERT INTO alert_signal_log (sent_at, kind, severity, symbol, label, title, body, results) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (_now(), signal.kind, signal.severity, signal.symbol, signal.label, signal.title, signal.body,
         json.dumps(results, ensure_ascii=False)))


def recent_signals(db: DB, limit: int = 200, symbol: str = None, severity: str = None) -> list:
    sql, params, conds = "SELECT * FROM alert_signal_log", [], []
    if symbol:
        conds.append("symbol = %s")
        params.append(symbol)
    if severity:
        conds.append("severity = %s")
        params.append(severity)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(int(limit))
    rows = db.fetchall(sql, params)
    for r in rows:
        r["results"] = json.loads(r["results"])
    return rows


# -- CLI -----------------------------------------------------------------------

def main(argv):
    cmd = argv[1] if len(argv) > 1 else "init"
    db = connect()
    if cmd == "init":
        print(f"tables ok: {MYSQL['host']}:{MYSQL['port']}/{MYSQL['database']} (alert_*)")
    elif cmd == "seed":
        from .config import SEED_WATCHLIST
        print(f"seeded {seed_watchlist(db, SEED_WATCHLIST)} symbols")
    else:
        raise SystemExit("usage: python -m alertbot.db [init|seed]")


if __name__ == "__main__":
    main(sys.argv)
