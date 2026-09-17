"""MySQL 저장소 — 워치리스트, 엔진 상태, 신호 이력.

엔진 워커와 백오피스가 사용자의 기존 MySQL(.env 의 MYSQL_*)을 공유한다. 같은 데이터베이스에
다른 프로젝트의 테이블이 있으므로 이름은 alert_ 접두어를 쓴다. 테스트는 같은 함수로
메모리 SQLite 를 쓴다 — 자리표시자(%s→?)와 upsert 문만 방언이 다르다.

    python -m alertbot.db init          테이블 생성
    python -m alertbot.db seed          config.SEED_WATCHLIST 의 종목을 넣는다 (이미 있으면 건너뜀)
    python -m alertbot.db reset-paper   실계좌가 섞였던 옛 모의매매 기록을 백업하고 비운다 (엔진·Binance 워커를 멈춘 뒤 한 번)
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
             account_id BIGINT,
             INDEX idx_alert_signal_sent (sent_at)
           ) CHARACTER SET utf8mb4""",
        """CREATE TABLE IF NOT EXISTS alert_binance_positions (
             id           BIGINT AUTO_INCREMENT PRIMARY KEY,
             mode         VARCHAR(8)  NOT NULL,
             strategy     VARCHAR(20) NOT NULL,
             symbol       VARCHAR(32) NOT NULL,
             side         VARCHAR(5)  NOT NULL,
             qty          DECIMAL(18,6) NOT NULL,
             entry_price  DECIMAL(18,6) NOT NULL,
             notional     DECIMAL(18,4) NOT NULL,
             leverage     DECIMAL(6,2)  NOT NULL,
             stop         DECIMAL(18,6) NOT NULL,
             take_profit  DECIMAL(18,6),
             deadline     VARCHAR(32) NOT NULL,
             signal_bar   BIGINT,
             next_funding BIGINT,
             funding      DECIMAL(18,6) NOT NULL DEFAULT 0,
             status       VARCHAR(8)  NOT NULL,
             exit_price   DECIMAL(18,6),
             exit_reason  VARCHAR(16),
             pnl          DECIMAL(18,4),
             opened_at    VARCHAR(32) NOT NULL,
             closed_at    VARCHAR(32),
             updated_at   VARCHAR(32),
             entry_order_id VARCHAR(32),
             stop_order_id  VARCHAR(32),
             account_id     BIGINT,
             INDEX idx_alert_bn_status (status)
           ) CHARACTER SET utf8mb4""",
        """CREATE TABLE IF NOT EXISTS alert_settings (
             k VARCHAR(64) PRIMARY KEY, v TEXT NOT NULL, updated_at VARCHAR(32) NOT NULL
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
             account_id BIGINT,
             INDEX idx_alert_orders_created (created_at),
             INDEX idx_alert_orders_symbol (symbol, status)
           ) CHARACTER SET utf8mb4""",
        # 백오피스 로그인 계정 = 허용 이메일 목록. live 스위치·금액 배율·Binance 자본도 계정별로 여기 둔다.
        """CREATE TABLE IF NOT EXISTS alert_accounts (
             id              BIGINT AUTO_INCREMENT PRIMARY KEY,
             email           VARCHAR(255) NOT NULL,
             role            VARCHAR(8)   NOT NULL,
             active          TINYINT      NOT NULL DEFAULT 1,
             google_sub      VARCHAR(64),
             toss_live       TINYINT      NOT NULL DEFAULT 0,
             binance_live    TINYINT      NOT NULL DEFAULT 0,
             amount_scale    DECIMAL(6,2)  NOT NULL DEFAULT 1,
             binance_capital DECIMAL(18,2) NOT NULL DEFAULT 0,
             created_at      VARCHAR(32)  NOT NULL,
             updated_at      VARCHAR(32)  NOT NULL,
             last_login_at   VARCHAR(32),
             UNIQUE KEY uq_alert_accounts_email (email)
           ) CHARACTER SET utf8mb4""",
        # 계정별 API 키 — 공급자 필드 JSON 을 통째로 AES-GCM 암호화한 문자열만 (alertbot/crypto.py)
        """CREATE TABLE IF NOT EXISTS alert_account_keys (
             account_id BIGINT      NOT NULL,
             provider   VARCHAR(16) NOT NULL,
             secret     TEXT        NOT NULL,
             updated_at VARCHAR(32) NOT NULL,
             PRIMARY KEY (account_id, provider)
           ) CHARACTER SET utf8mb4""",
        # 로그인 세션. 원문 토큰은 쿠키에만 있고 여기엔 sha256 만 둔다
        """CREATE TABLE IF NOT EXISTS alert_sessions (
             token_hash CHAR(64)    NOT NULL PRIMARY KEY,
             account_id BIGINT      NOT NULL,
             csrf       VARCHAR(64) NOT NULL,
             created_at VARCHAR(32) NOT NULL,
             expires_at VARCHAR(32) NOT NULL,
             INDEX idx_alert_sessions_expires (expires_at)
           ) CHARACTER SET utf8mb4""",
        # run.py(관리 프로세스)의 서비스 상태이자 제어 요청함 — 백오피스 '운영' 화면·python run.py start|stop|restart 가 request 를 쓴다
        """CREATE TABLE IF NOT EXISTS alert_services (
             name         VARCHAR(16) PRIMARY KEY,
             state        VARCHAR(10) NOT NULL,
             pid          INT,
             host         VARCHAR(64),
             started_at   VARCHAR(32),
             heartbeat_at VARCHAR(32),
             exit_code    INT,
             restarts     INT NOT NULL DEFAULT 0,
             last_output  TEXT,
             request      VARCHAR(8),
             requested_by VARCHAR(255),
             requested_at VARCHAR(32),
             updated_at   VARCHAR(32) NOT NULL
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
             results TEXT NOT NULL, account_id INTEGER)""",
        "CREATE INDEX IF NOT EXISTS idx_alert_signal_sent ON alert_signal_log (sent_at)",
        """CREATE TABLE IF NOT EXISTS alert_binance_positions (
             id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL, strategy TEXT NOT NULL, symbol TEXT NOT NULL,
             side TEXT NOT NULL, qty REAL NOT NULL, entry_price REAL NOT NULL, notional REAL NOT NULL,
             leverage REAL NOT NULL, stop REAL NOT NULL, take_profit REAL, deadline TEXT NOT NULL, signal_bar INTEGER, next_funding INTEGER,
             funding REAL NOT NULL DEFAULT 0, status TEXT NOT NULL, exit_price REAL, exit_reason TEXT, pnl REAL,
             opened_at TEXT NOT NULL, closed_at TEXT, updated_at TEXT, entry_order_id TEXT, stop_order_id TEXT,
             account_id INTEGER)""",
        "CREATE TABLE IF NOT EXISTS alert_settings (k TEXT PRIMARY KEY, v TEXT NOT NULL, updated_at TEXT NOT NULL)",
        """CREATE TABLE IF NOT EXISTS alert_orders (
             intent_id TEXT PRIMARY KEY, mode TEXT NOT NULL, symbol TEXT NOT NULL, market TEXT NOT NULL,
             side TEXT NOT NULL, kind TEXT NOT NULL, order_type TEXT NOT NULL, price REAL NOT NULL,
             quantity REAL NOT NULL, amount REAL NOT NULL, bar_key TEXT, ref_avg REAL, status TEXT NOT NULL,
             reason TEXT, order_id TEXT, filled_qty REAL NOT NULL DEFAULT 0, avg_price REAL, pnl REAL,
             created_at TEXT NOT NULL, updated_at TEXT, account_id INTEGER)""",
        """CREATE TABLE IF NOT EXISTS alert_accounts (
             id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, role TEXT NOT NULL,
             active INTEGER NOT NULL DEFAULT 1, google_sub TEXT, toss_live INTEGER NOT NULL DEFAULT 0,
             binance_live INTEGER NOT NULL DEFAULT 0, amount_scale REAL NOT NULL DEFAULT 1,
             binance_capital REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_login_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS alert_account_keys (
             account_id INTEGER NOT NULL, provider TEXT NOT NULL, secret TEXT NOT NULL, updated_at TEXT NOT NULL,
             PRIMARY KEY (account_id, provider))""",
        """CREATE TABLE IF NOT EXISTS alert_sessions (
             token_hash TEXT PRIMARY KEY, account_id INTEGER NOT NULL, csrf TEXT NOT NULL,
             created_at TEXT NOT NULL, expires_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS alert_services (
             name TEXT PRIMARY KEY, state TEXT NOT NULL, pid INTEGER, host TEXT, started_at TEXT, heartbeat_at TEXT,
             exit_code INTEGER, restarts INTEGER NOT NULL DEFAULT 0, last_output TEXT, request TEXT, requested_by TEXT,
             requested_at TEXT, updated_at TEXT NOT NULL)""",
    ],
}

# 기존 테이블에 나중에 추가된 컬럼. init_schema 가 없으면 붙인다.
EXTRA_COLUMNS = {
    "alert_watchlist": {
        "mysql": [("auto_trade", "TINYINT NOT NULL DEFAULT 0"), ("auto_amount", "DECIMAL(18,2) NOT NULL DEFAULT 0"),
                  ("day_trade", "TINYINT NOT NULL DEFAULT 0")],
        "sqlite": [("auto_trade", "INTEGER NOT NULL DEFAULT 0"), ("auto_amount", "REAL NOT NULL DEFAULT 0"),
                   ("day_trade", "INTEGER NOT NULL DEFAULT 0")],
    },
    "alert_binance_positions": {                     # live 주문번호 (dry 행은 비어 있다), 계정 (NULL = 공용 가상 장부), 목표가 (없으면 NULL)
        "mysql": [("entry_order_id", "VARCHAR(32)"), ("stop_order_id", "VARCHAR(32)"), ("account_id", "BIGINT"),
                  ("take_profit", "DECIMAL(18,6)")],
        "sqlite": [("entry_order_id", "TEXT"), ("stop_order_id", "TEXT"), ("account_id", "INTEGER"), ("take_profit", "REAL")],
    },
    "alert_orders": {                                # 계정 (NULL = 공용 가상 장부)
        "mysql": [("account_id", "BIGINT")],
        "sqlite": [("account_id", "INTEGER")],
    },
    "alert_signal_log": {                            # 계정별 알림이면 그 계정 (NULL = 공용 채널)
        "mysql": [("account_id", "BIGINT")],
        "sqlite": [("account_id", "INTEGER")],
    },
}

# 조회 패턴에 맞춘 인덱스. 나중에 생긴 것이라 init_schema 가 없을 때만 만든다 — 계정·모드·상태·시각으로 거르고,
# 알림 이력은 종류·종목·계정별 최신순(ORDER BY id DESC LIMIT)으로 읽는다.
EXTRA_INDEXES = {
    "idx_alert_orders_account": ("alert_orders", "account_id, mode, status, created_at"),
    "idx_alert_bn_account": ("alert_binance_positions", "account_id, mode, status"),
    "idx_alert_signal_kind": ("alert_signal_log", "kind, id"),
    "idx_alert_signal_symbol": ("alert_signal_log", "symbol, id"),
    "idx_alert_signal_account": ("alert_signal_log", "account_id, id"),
}

# 자동매매 운영 설정 기본값. 백오피스(관리자)에서 바꾸고 엔진이 매 사이클 읽는다. 한도는 live 계정마다 따로 집계하고 가상 장부에는 적용하지 않는다.
# live 스위치는 전역 킬 스위치가 아니라 계정별이다 (alert_accounts.toss_live / binance_live).
SETTING_DEFAULTS = {
    "max_positions": "3",                # 동시 보유 종목 수 상한 (열린 매수 의도 포함)
    "max_orders_per_day": "20",          # 하루 주문 횟수 상한 (손절 매도는 면제)
    "daily_loss_limit_krw": "300000",    # 오늘 실현손실이 이 아래면 매수 중단 (원)
    "daily_loss_limit_usd": "200",       # 같은 기준 (달러)
    "max_order_amount_krw": "1000000",   # 1회 매수 금액 상한 (원)
    "max_order_amount_usd": "1000",      # 1회 매수 금액 상한 (달러)
    "binance_signal_book": "[]",         # 코인 신호 포지션 장부(binance_book.SignalBook) — 워커가 쓰는 JSON, 백오피스는 건드리지 않는다
    "binance_scan_include": "",          # 급변 감시에 더할 코인 (쉼표 목록, 관리자) — alertbot/binance_scan.py
    "binance_scan_exclude": "",          # 급변 감시에서 뺄 코인 (쉼표 목록, 관리자)
    "binance_scan_universe": "{}",       # 워커가 고른 급변 감시 목록 스냅샷 (JSON) — 백오피스 표시용
    "binance_scan_fade_pending": "{}",   # 급등 소진 숏 대기 목록 {심볼: {deadline, after}} (JSON) — 워커가 쓴다, 재시작해도 이어진다
    "binance_scan_last_alert": "{}",     # 급변 감시 코인별 마지막 알림 시각 {심볼: ms} (JSON) — 재시작해도 쿨다운이 이어진다
}

# upsert 는 방언이 다르다. MySQL 은 8.0.19+ 의 행 별칭(AS new) 구문 — VALUES() 는 8.0.20 부터 폐기 예정.
UPSERT_WATCH = {
    "mysql": """INSERT INTO alert_watchlist
                  (symbol, market, name, leaders, inverse, pair, hold_only, note, enabled, auto_trade, auto_amount,
                   day_trade, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) AS new
                ON DUPLICATE KEY UPDATE market=new.market, name=new.name, leaders=new.leaders,
                  inverse=new.inverse, pair=new.pair, hold_only=new.hold_only, note=new.note,
                  enabled=new.enabled, auto_trade=new.auto_trade, auto_amount=new.auto_amount,
                  day_trade=new.day_trade, updated_at=new.updated_at""",
    "sqlite": """INSERT INTO alert_watchlist
                  (symbol, market, name, leaders, inverse, pair, hold_only, note, enabled, auto_trade, auto_amount,
                   day_trade, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(symbol) DO UPDATE SET market=excluded.market, name=excluded.name,
                  leaders=excluded.leaders, inverse=excluded.inverse, pair=excluded.pair,
                  hold_only=excluded.hold_only, note=excluded.note, enabled=excluded.enabled,
                  auto_trade=excluded.auto_trade, auto_amount=excluded.auto_amount,
                  day_trade=excluded.day_trade, updated_at=excluded.updated_at""",
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
    """엔진 상태에 섞인 datetime 은 ISO 문자열로. 복원 쪽은 문자열을 그대로 파싱한다 (pending.at·next_at)."""
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
        # 읽기·쓰기 timeout: 반쯤 끊긴 연결에서 응답을 영원히 기다리지 않게 (끊기면 execute 가 재연결해 한 번 더 시도한다)
        return pymysql.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"],
                               password=cfg["password"], database=cfg["database"],
                               charset="utf8mb4", autocommit=True, connect_timeout=10, read_timeout=30, write_timeout=30,
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
        self._add_missing_indexes()
        self._widen_settings()
        return self

    def _widen_settings(self):
        """alert_settings.v 는 처음에 VARCHAR(255) 였다 — 신호 포지션 장부·코인 목록 JSON 이 넘친다. 기존 MySQL 표만 TEXT 로 넓힌다."""
        if self.dialect != "mysql":
            return
        col = self.fetchone("SHOW COLUMNS FROM alert_settings LIKE 'v'")
        if col and str(col["Type"]).lower().startswith("varchar"):
            self.execute("ALTER TABLE alert_settings MODIFY v TEXT NOT NULL")
            log.info("alert_settings.v 를 TEXT 로 넓혔다")

    def _add_missing_columns(self):
        """기존 테이블에 나중에 생긴 컬럼이 없으면 붙인다 (있으면 아무것도 안 한다)."""
        for table, cols in EXTRA_COLUMNS.items():
            if self.dialect == "mysql":
                have = {r["Field"] for r in self.fetchall(f"SHOW COLUMNS FROM {table}")}
            else:
                have = {r["name"] for r in self.fetchall(f"PRAGMA table_info({table})")}
            for col, ddl in cols[self.dialect]:
                if col not in have:
                    self.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                    log.info("%s.%s 컬럼 추가", table, col)

    def _add_missing_indexes(self):
        """EXTRA_INDEXES 중 없는 것만 만든다. MySQL 은 CREATE INDEX IF NOT EXISTS 가 없어 SHOW INDEX 로 확인한다."""
        for name, (table, cols) in EXTRA_INDEXES.items():
            if self.dialect == "mysql":
                if any(r["Key_name"] == name for r in self.fetchall(f"SHOW INDEX FROM {table}")):
                    continue
                self.execute(f"CREATE INDEX {name} ON {table} ({cols})")
                log.info("%s 인덱스 추가 (%s)", name, table)
            else:
                self.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})")

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
        "day_trade": bool(row.get("day_trade") or 0),
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
                 note: str = None, enabled: bool = True, auto_trade: bool = False, auto_amount: float = 0,
                 day_trade: bool = False):
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
                int(bool(enabled)), int(bool(auto_trade)), float(auto_amount or 0), int(bool(day_trade)), now, now))


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
                     auto_trade=cfg.get("auto_trade", False), auto_amount=cfg.get("auto_amount", 0),
                     day_trade=cfg.get("day_trade", False))
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

def _book(account_id) -> tuple:
    """장부 조건 — None 은 공용 가상 장부(dry·계정 없음), 숫자는 그 계정의 live. 실행기·트레이더가 자기 장부만 본다.
    모드도 보는 이유: 계정 컬럼이 생기기 전의 live 행(account_id NULL)이 가상 장부에 섞이면 안 된다."""
    if account_id is None:
        return "mode = 'dry' AND account_id IS NULL", ()
    return "mode = 'live' AND account_id = %s", (int(account_id),)


def _visible(account_ids) -> tuple:
    """화면 필터 — None 이면 전부(관리자), 목록이면 그 장부들만(None 은 공용). 일반 계정은 [None, 내 id]."""
    if account_ids is None:
        return "", ()
    ids = [int(a) for a in account_ids if a is not None]
    parts = (["account_id IS NULL"] if None in account_ids else []) + \
            ([f"account_id IN ({', '.join(['%s'] * len(ids))})"] if ids else [])
    return "(" + (" OR ".join(parts) or "1 = 0") + ")", tuple(ids)


def log_signal(db: DB, signal, results: dict):
    db.execute(
        "INSERT INTO alert_signal_log (sent_at, kind, severity, symbol, label, title, body, results, account_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (_now(), signal.kind, signal.severity, signal.symbol, signal.label, signal.title, signal.full_body(),
         json.dumps(results, ensure_ascii=False), getattr(signal, "account_id", None)))


def recent_signals(db: DB, limit: int = 200, symbol: str = None, severity: str = None, kind: str = None,
                   account_ids: list = None) -> list:
    sql, params, conds = "SELECT * FROM alert_signal_log", [], []
    visible, ids = _visible(account_ids)
    if visible:
        conds.append(visible)
        params.extend(ids)
    if symbol:
        conds.append("symbol = %s")
        params.append(symbol)
    if severity:
        conds.append("severity = %s")
        params.append(severity)
    if kind:
        conds.append("kind = %s")
        params.append(kind)
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
                 "created_at", "updated_at", "account_id")


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


def open_orders(db: DB, symbol: str = None, account_id: int = None) -> list:
    """이 장부(None = 공용 가상)의 미결 의도."""
    book, params = _book(account_id)
    sql, params = f"SELECT * FROM alert_orders WHERE status IN ('sent', 'open') AND {book}", list(params)
    if symbol:
        sql += " AND symbol = %s"
        params.append(symbol)
    return _order_rows(db, sql + " ORDER BY created_at", params)


def orders_since(db: DB, since_iso: str, mode: str = None, account_id: int = None) -> list:
    """이 장부에서 since_iso(UTC ISO) 이후 생성된 의도. 하루 주문 수·실현손익 집계용 — 계정마다 따로 센다."""
    book, ids = _book(account_id)
    sql, params = f"SELECT * FROM alert_orders WHERE {book} AND created_at >= %s", [*ids, since_iso]
    if mode:
        sql += " AND mode = %s"
        params.append(mode)
    return _order_rows(db, sql + " ORDER BY created_at", params)


def recent_orders(db: DB, limit: int = 200, account_ids: list = None) -> list:
    visible, ids = _visible(account_ids)
    return _order_rows(db, "SELECT * FROM alert_orders" + (f" WHERE {visible}" if visible else "")
                       + " ORDER BY created_at DESC LIMIT %s", (*ids, int(limit)))


def get_order(db: DB, intent_id: str):
    rows = _order_rows(db, "SELECT * FROM alert_orders WHERE intent_id = %s", (intent_id,))
    return rows[0] if rows else None


def trade_rows(db: DB, mode: str = None, account_ids: list = None) -> list:
    """체결된 매도(실현손익 있음) — 매매 결과 화면의 건별·일별 시계열. 오래된 것부터."""
    sql = "SELECT * FROM alert_orders WHERE side = 'SELL' AND status IN ('filled', 'partial') AND pnl IS NOT NULL"
    visible, ids = _visible(account_ids)
    params = list(ids)
    if visible:
        sql += f" AND {visible}"
    if mode:
        sql += " AND mode = %s"
        params.append(mode)
    return _order_rows(db, sql + " ORDER BY created_at", params)


def dry_positions(db: DB) -> dict:
    """공용 가상 장부의 모의 보유 — symbol -> {qty, avg, market}. 매수는 평단을 가중 평균으로 더하고 매도는 수량을 뺀다.

    따로 표를 두지 않고 체결된 가상 의도(dry, 계정 없음)에서 매번 계산한다 — 이중 장부가 없어 어긋날 수 없고, 주문 수는 하루 수십 건이 상한이다.
    실계좌 보유는 섞지 않는다 (가상매매는 실계좌와 격리).
    """
    pos = {}
    rows = _order_rows(db, "SELECT * FROM alert_orders WHERE account_id IS NULL AND mode = 'dry' "
                           "AND status IN ('filled', 'partial') ORDER BY created_at")
    for o in rows:
        qty, price = o["filled_qty"] or 0.0, o["avg_price"] or o["price"]
        if qty <= 0:
            continue
        p = pos.setdefault(o["symbol"], {"qty": 0.0, "avg": 0.0, "market": o["market"]})
        if o["side"] == "BUY":
            p["avg"] = round((p["avg"] * p["qty"] + price * qty) / (p["qty"] + qty), 4)
            p["qty"] += qty
        else:
            p["qty"] = max(p["qty"] - qty, 0.0)
    return {s: p for s, p in pos.items() if p["qty"] > 0}


# -- Binance 가상 포지션 (alertbot/binance_trade.py) --------------------------------

BN_COLUMNS = ("mode", "strategy", "symbol", "side", "qty", "entry_price", "notional", "leverage", "stop", "take_profit", "deadline",
              "signal_bar", "next_funding", "funding", "status", "opened_at", "entry_order_id", "stop_order_id", "account_id")
BN_FLOATS = ("qty", "entry_price", "notional", "leverage", "stop", "take_profit", "funding", "exit_price", "pnl")


def insert_binance_position(db: DB, row: dict) -> int:
    cols, marks = ", ".join(BN_COLUMNS), ", ".join(["%s"] * len(BN_COLUMNS))
    cur = db.execute(f"INSERT INTO alert_binance_positions ({cols}) VALUES ({marks})",
                     tuple(row.get(c) for c in BN_COLUMNS))
    return int(cur.lastrowid)


def update_binance_position(db: DB, pos_id: int, **fields):
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k} = %s" for k in fields)
    db.execute(f"UPDATE alert_binance_positions SET {sets} WHERE id = %s", (*fields.values(), pos_id))


def binance_positions(db: DB, status: str = None, limit: int = 200, account_ids: list = None) -> list:
    conds, params = [], []
    if status:
        conds, params = ["status = %s"], [status]
    visible, ids = _visible(account_ids)
    if visible:
        conds.append(visible)
        params.extend(ids)
    sql = "SELECT * FROM alert_binance_positions" + (" WHERE " + " AND ".join(conds) if conds else "")
    rows = db.fetchall(sql + " ORDER BY id DESC LIMIT %s", params + [int(limit)])
    for r in rows:
        for k in BN_FLOATS:
            if r.get(k) is not None:
                r[k] = float(r[k])
    return rows


def binance_pnl_since(db: DB, since_iso: str, mode: str, account_id: int = None) -> float:
    """이 장부(None = 공용 가상)에서 since_iso(UTC ISO) 이후 종료된 포지션의 실현손익 합 — 일손실 한도용."""
    book, ids = _book(account_id)
    row = db.fetchone("SELECT COALESCE(SUM(pnl), 0) AS s FROM alert_binance_positions "
                      f"WHERE status = 'closed' AND mode = %s AND {book} AND closed_at >= %s", (mode, *ids, since_iso))
    return float(row["s"] or 0)


# -- 서비스 (run.py 관리 프로세스) ---------------------------------------------------------

def ensure_services(db: DB, names):
    """없는 서비스 행만 만든다 (관리 프로세스가 lease 를 확인한 뒤 한 번)."""
    have = {r["name"] for r in db.fetchall("SELECT name FROM alert_services")}
    for name in names:
        if name not in have:
            db.execute("INSERT INTO alert_services (name, state, updated_at) VALUES (%s, 'stopped', %s)", (name, _now()))


def update_service(db: DB, name: str, **fields):
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k} = %s" for k in fields)
    db.execute(f"UPDATE alert_services SET {sets} WHERE name = %s", (*fields.values(), name))


def touch_service(db: DB, name: str):
    """워커 heartbeat — 사이클마다. 행이 없으면(관리 프로세스 없이 단독 실행) 아무 일도 없다."""
    db.execute("UPDATE alert_services SET heartbeat_at = %s WHERE name = %s", (_now(), name))


def request_service(db: DB, name: str, action: str, by: str):
    """start|stop|restart 요청 — 관리 프로세스가 몇 초 안에 읽어 처리하고 지운다."""
    db.execute("UPDATE alert_services SET request = %s, requested_by = %s, requested_at = %s WHERE name = %s",
               (action, by, _now(), name))


def clear_request(db: DB, name: str, requested_at: str):
    """처리한 요청만 지운다 — 그 사이 새 요청이 들어왔으면(requested_at 이 다르면) 남긴다."""
    db.execute("UPDATE alert_services SET request = NULL WHERE name = %s AND requested_at = %s", (name, requested_at))


def list_services(db: DB) -> dict:
    return {r["name"]: r for r in db.fetchall("SELECT * FROM alert_services ORDER BY name")}


# -- 옛 모의매매 기록 백업·초기화 --------------------------------------------------------

def reset_paper(db: DB, data_dir, stamp: str = None, admin_email: str = None) -> dict:
    """실계좌 보유가 섞였던 옛 dry 기록을 백업 표·파일로 옮기고 비운다. 격리된 가상 장부는 빈 상태에서 다시 쌓인다.

    - alert_orders / alert_binance_positions 의 dry 행 → <표>_paper_bak_<stamp> 로 복사 후 삭제 (live 행은 그대로)
    - 엔진 상태의 종목별 last_seen(실계좌 평단·수량) 삭제 — 남기면 첫 사이클에 가짜 '청산 완료' 가 나간다
    - trade_log.csv → trade_log.paper-bak-<stamp>.csv (신호 성적 CSV 는 실계좌와 무관해 그대로)
    - 계정 컬럼이 생기기 전의 live 행(.env 키로 낸 실제 주문·포지션)은 관리자 계정(admin_email) 것으로 옮긴다 — 계정 트레이더가 이어서 관리한다
    """
    from pathlib import Path
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = {}
    for table in ("alert_orders", "alert_binance_positions"):
        n = db.fetchone(f"SELECT COUNT(*) AS n FROM {table} WHERE mode = 'dry'")["n"]
        if n:
            db.execute(f"CREATE TABLE {table}_paper_bak_{stamp} AS SELECT * FROM {table} WHERE mode = 'dry'")
            db.execute(f"DELETE FROM {table} WHERE mode = 'dry'")
        out[table] = int(n)
    st = load_engine_status(db)
    if st:
        cleared = [t for t, item in st["state"].items() if item.pop("last_seen", None)]
        if cleared:
            db.execute("UPDATE alert_engine_status SET state = %s WHERE id = 1",
                       (json.dumps(st["state"], ensure_ascii=False, default=_json_default),))
        out["last_seen"] = len(cleared)
    if admin_email:
        from . import accounts
        admin = accounts.ensure_admin(db, admin_email)
        for table in ("alert_orders", "alert_binance_positions"):
            n = db.fetchone(f"SELECT COUNT(*) AS n FROM {table} WHERE mode = 'live' AND account_id IS NULL")["n"]
            db.execute(f"UPDATE {table} SET account_id = %s WHERE mode = 'live' AND account_id IS NULL", (admin,))
            out[f"{table}_live_to_admin"] = int(n)
    trade_log = Path(data_dir) / "trade_log.csv"
    out["trade_log"] = None
    if trade_log.exists():
        backup = trade_log.with_name(f"trade_log.paper-bak-{stamp}.csv")
        trade_log.rename(backup)
        out["trade_log"] = backup.name
    return out


# -- CLI -----------------------------------------------------------------------

def main(argv):
    cmd = argv[1] if len(argv) > 1 else "init"
    db = connect()
    if cmd == "init":
        print(f"tables ok: {MYSQL['host']}:{MYSQL['port']}/{MYSQL['database']} (alert_*)")
    elif cmd == "seed":
        from .config import SEED_WATCHLIST
        print(f"seeded {seed_watchlist(db, SEED_WATCHLIST)} symbols")
    elif cmd == "reset-paper":
        from .config import ADMIN_EMAIL, DATA_DIR
        print(f"옛 모의매매 기록 백업·초기화: {reset_paper(db, DATA_DIR, admin_email=ADMIN_EMAIL)}")
    else:
        raise SystemExit("usage: python -m alertbot.db [init|seed|reset-paper]")


if __name__ == "__main__":
    main(sys.argv)
