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
        """CREATE TABLE IF NOT EXISTS alert_settings (
             k VARCHAR(64) PRIMARY KEY, v VARCHAR(255) NOT NULL, updated_at VARCHAR(32) NOT NULL
           ) CHARACTER SET utf8mb4""",
        """CREATE TABLE IF NOT EXISTS alert_orders (
             intent_id  VARCHAR(40) PRIMARY KEY,
             mode       VARCHAR(4)  NOT NULL,
             symbol     VARCHAR(32) NOT NULL,
             market     VARCHAR(2)  NOT NULL,
             side       VARCHAR(4)  NOT NULL,
             kind       VARCHAR(20) NOT NULL,
             order_type VARCHAR(6)  NOT NULL,
             price      DECIMAL(18,4) NOT NULL,
             quantity   DECIMAL(18,6) NOT NULL,
             amount     DECIMAL(18,4) NOT NULL,
             bar_key    VARCHAR(40),
             ref_avg    DECIMAL(18,4),
             status     VARCHAR(10) NOT NULL,
             reason     VARCHAR(255),
             order_id   VARCHAR(64),
             filled_qty DECIMAL(18,6) NOT NULL DEFAULT 0,
             avg_price  DECIMAL(18,4),
             pnl        DECIMAL(18,4),
             created_at VARCHAR(32) NOT NULL,
             updated_at VARCHAR(32),
             INDEX idx_alert_orders_created (created_at),
             INDEX idx_alert_orders_symbol (symbol, status)
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
        "CREATE TABLE IF NOT EXISTS alert_settings (k TEXT PRIMARY KEY, v TEXT NOT NULL, updated_at TEXT NOT NULL)",
        """CREATE TABLE IF NOT EXISTS alert_orders (
             intent_id TEXT PRIMARY KEY, mode TEXT NOT NULL, symbol TEXT NOT NULL, market TEXT NOT NULL,
             side TEXT NOT NULL, kind TEXT NOT NULL, order_type TEXT NOT NULL, price REAL NOT NULL,
             quantity REAL NOT NULL, amount REAL NOT NULL, bar_key TEXT, ref_avg REAL, status TEXT NOT NULL,
             reason TEXT, order_id TEXT, filled_qty REAL NOT NULL DEFAULT 0, avg_price REAL, pnl REAL,
             created_at TEXT NOT NULL, updated_at TEXT)""",
    ],
}

# 기존 테이블에 나중에 추가된 컬럼. init_schema 가 없으면 붙인다.
WATCHLIST_EXTRA_COLUMNS = {
    "mysql": [("auto_trade", "TINYINT NOT NULL DEFAULT 0"), ("auto_amount", "DECIMAL(18,2) NOT NULL DEFAULT 0")],
    "sqlite": [("auto_trade", "INTEGER NOT NULL DEFAULT 0"), ("auto_amount", "REAL NOT NULL DEFAULT 0")],
}

# 자동매매 운영 설정 기본값. 백오피스에서 바꾸고 엔진이 매 사이클 읽는다.
SETTING_DEFAULTS = {
    "autotrade_enabled": "0",            # 킬 스위치. 1 이어야 주문이 나간다 (.env AUTOTRADE_MODE 와 별개)
    "max_positions": "3",                # 동시 보유 종목 수 상한 (열린 매수 의도 포함)
    "max_orders_per_day": "20",          # 하루 주문 횟수 상한 (손절 매도는 면제)
    "daily_loss_limit_krw": "300000",    # 오늘 실현손실이 이 아래면 매수 중단 (원)
    "daily_loss_limit_usd": "200",       # 같은 기준 (달러)
    "max_order_amount_krw": "1000000",   # 1회 매수 금액 상한 (원)
    "max_order_amount_usd": "1000",      # 1회 매수 금액 상한 (달러)
}

# upsert 는 방언이 다르다. MySQL 은 8.0.19+ 의 행 별칭(AS new) 구문 — VALUES() 는 8.0.20 부터 폐기 예정.
UPSERT_WATCH = {
    "mysql": """INSERT INTO alert_watchlist
                  (symbol, market, name, leaders, inverse, pair, hold_only, note, enabled, auto_trade, auto_amount,
                   created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) AS new
                ON DUPLICATE KEY UPDATE market=new.market, name=new.name, leaders=new.leaders,
                  inverse=new.inverse, pair=new.pair, hold_only=new.hold_only, note=new.note,
                  enabled=new.enabled, auto_trade=new.auto_trade, auto_amount=new.auto_amount,
                  updated_at=new.updated_at""",
    "sqlite": """INSERT INTO alert_watchlist
                  (symbol, market, name, leaders, inverse, pair, hold_only, note, enabled, auto_trade, auto_amount,
                   created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(symbol) DO UPDATE SET market=excluded.market, name=excluded.name,
                  leaders=excluded.leaders, inverse=excluded.inverse, pair=excluded.pair,
                  hold_only=excluded.hold_only, note=excluded.note, enabled=excluded.enabled,
                  auto_trade=excluded.auto_trade, auto_amount=excluded.auto_amount,
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


def _json_default(value):
    """엔진 상태에 섞인 datetime 은 ISO 문자열로. 복원 쪽은 문자열이어도 쓰지 않는 필드(pending.at)다."""
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"JSON 직렬화 불가: {type(value).__name__}")


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
        # autocommit. check_same_thread=False: 백오피스 테스트가 워커 스레드에서 같은 연결을 쓴다 (락으로 직렬화)
        con = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
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
        self._add_missing_columns()
        return self

    def _add_missing_columns(self):
        """기존 alert_watchlist 에 자동매매 컬럼이 없으면 붙인다 (있으면 아무것도 안 한다)."""
        if self.dialect == "mysql":
            have = {r["Field"] for r in self.fetchall("SHOW COLUMNS FROM alert_watchlist")}
        else:
            have = {r["name"] for r in self.fetchall("PRAGMA table_info(alert_watchlist)")}
        for col, ddl in WATCHLIST_EXTRA_COLUMNS[self.dialect]:
            if col not in have:
                self.execute(f"ALTER TABLE alert_watchlist ADD COLUMN {col} {ddl}")
                log.info("alert_watchlist.%s 컬럼 추가", col)

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
        "auto_trade": bool(row.get("auto_trade") or 0),
        "auto_amount": float(row.get("auto_amount") or 0),
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
                 note: str = None, enabled: bool = True, auto_trade: bool = False, auto_amount: float = 0):
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
                int(bool(enabled)), int(bool(auto_trade)), float(auto_amount or 0), now, now))


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
                     cfg.get("inverse", False), cfg.get("pair"), cfg.get("hold_only", False), cfg.get("note"),
                     auto_trade=cfg.get("auto_trade", False), auto_amount=cfg.get("auto_amount", 0))
        added += 1
    return added


# -- 엔진 상태 -----------------------------------------------------------------

def save_engine_status(db: DB, active: list, pre: list, state: dict, snapshots: dict, last_error: str = None):
    db.execute(UPSERT_STATUS[db.dialect],
               (_now(), json.dumps(active), json.dumps(pre), last_error,
                json.dumps(state, ensure_ascii=False, default=_json_default),
                json.dumps(snapshots, ensure_ascii=False, default=_json_default)))


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


# -- 자동매매 설정 -------------------------------------------------------------

def get_settings(db: DB) -> dict:
    """기본값 위에 저장된 값을 덮는다. 없는 키는 기본값이다."""
    out = dict(SETTING_DEFAULTS)
    for r in db.fetchall("SELECT k, v FROM alert_settings"):
        out[r["k"]] = r["v"]
    return out


def set_setting(db: DB, key: str, value):
    if key not in SETTING_DEFAULTS:
        raise ValueError(f"알 수 없는 설정: {key}")
    sql = {
        "mysql": "INSERT INTO alert_settings (k, v, updated_at) VALUES (%s, %s, %s) AS new "
                 "ON DUPLICATE KEY UPDATE v=new.v, updated_at=new.updated_at",
        "sqlite": "INSERT INTO alert_settings (k, v, updated_at) VALUES (%s, %s, %s) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
    }[db.dialect]
    db.execute(sql, (key, str(value), _now()))


# -- 주문 의도 -----------------------------------------------------------------

ORDER_COLUMNS = ("intent_id", "mode", "symbol", "market", "side", "kind", "order_type", "price", "quantity",
                 "amount", "bar_key", "ref_avg", "status", "reason", "order_id", "filled_qty", "avg_price", "pnl",
                 "created_at", "updated_at")


def insert_order(db: DB, row: dict):
    cols = ", ".join(ORDER_COLUMNS)
    marks = ", ".join(["%s"] * len(ORDER_COLUMNS))
    db.execute(f"INSERT INTO alert_orders ({cols}) VALUES ({marks})", tuple(row.get(c) for c in ORDER_COLUMNS))


def update_order(db: DB, intent_id: str, **fields):
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k} = %s" for k in fields)
    db.execute(f"UPDATE alert_orders SET {sets} WHERE intent_id = %s", tuple(fields.values()) + (intent_id,))


def _order_rows(db: DB, sql: str, params=()) -> list:
    rows = db.fetchall(sql, params)
    for r in rows:
        for k in ("price", "quantity", "amount", "ref_avg", "filled_qty", "avg_price", "pnl"):
            if r.get(k) is not None:
                r[k] = float(r[k])
    return rows


def open_orders(db: DB, symbol: str = None) -> list:
    sql = "SELECT * FROM alert_orders WHERE status IN ('sent', 'open')"
    params = []
    if symbol:
        sql += " AND symbol = %s"
        params.append(symbol)
    return _order_rows(db, sql + " ORDER BY created_at", params)


def orders_since(db: DB, since_iso: str, mode: str = None) -> list:
    """since_iso(UTC ISO) 이후 생성된 의도. 하루 주문 수·실현손익 집계용."""
    sql, params = "SELECT * FROM alert_orders WHERE created_at >= %s", [since_iso]
    if mode:
        sql += " AND mode = %s"
        params.append(mode)
    return _order_rows(db, sql + " ORDER BY created_at", params)


def recent_orders(db: DB, limit: int = 200) -> list:
    return _order_rows(db, "SELECT * FROM alert_orders ORDER BY created_at DESC LIMIT %s", (int(limit),))


def get_order(db: DB, intent_id: str):
    rows = _order_rows(db, "SELECT * FROM alert_orders WHERE intent_id = %s", (intent_id,))
    return rows[0] if rows else None


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
