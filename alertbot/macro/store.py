"""매크로 저장소 — alert_macro_series·events·scenarios·scenario_log. 함수는 db.py 와 같은 fn(db, ...) 모양이다.

시계열은 (series_key, obs_date) 당 한 값이다. 같은 날짜에 여러 출처가 오면 우선순위가 높거나 같은 쪽이 덮는다:
관리자 입력(manual) > 공식(fred·mof) > 시세(yahoo). 그래서 Yahoo 장중 값은 FRED 종가가 오면 밀려나고, 관리자가 고친 값은 수집이 덮지 않는다.
"""

import json
from datetime import datetime, timezone

from ..db import DB, _now

SOURCE_RANK = {"yahoo": 1, "fred": 2, "mof": 2, "seed": 2, "manual": 3}


def _rank_sql(expr: str) -> str:
    return f"(CASE {expr} WHEN 'manual' THEN 3 WHEN 'yahoo' THEN 1 ELSE 2 END)"


def upsert_series(db: DB, rows: list, source: str, note: str = None) -> int:
    """rows = [(series_key, 'YYYY-MM-DD', value)]. 반환: 넘긴 행 수."""
    if not rows:
        return 0
    now = _now()
    if db.dialect == "mysql":
        t, n = "alert_macro_series", "new"
        win = f"{_rank_sql(n + '.source')} >= {_rank_sql(t + '.source')}"
        # MySQL 은 SET 을 왼쪽부터 적용한다 — source 를 맨 뒤에 바꿔야 앞의 비교가 옛 출처를 본다
        sql = (f"INSERT INTO {t} (series_key, obs_date, value, source, fetched_at, note) VALUES {{values}} AS new "
               f"ON DUPLICATE KEY UPDATE value=IF({win}, new.value, {t}.value), note=IF({win}, new.note, {t}.note), "
               f"fetched_at=IF({win}, new.fetched_at, {t}.fetched_at), source=IF({win}, new.source, {t}.source)")
    else:
        t = "alert_macro_series"
        win = f"{_rank_sql('excluded.source')} >= {_rank_sql(t + '.source')}"
        sql = (f"INSERT INTO {t} (series_key, obs_date, value, source, fetched_at, note) VALUES (%s, %s, %s, %s, %s, %s) "
               f"ON CONFLICT(series_key, obs_date) DO UPDATE SET "
               f"value=CASE WHEN {win} THEN excluded.value ELSE {t}.value END, "
               f"note=CASE WHEN {win} THEN excluded.note ELSE {t}.note END, "
               f"fetched_at=CASE WHEN {win} THEN excluded.fetched_at ELSE {t}.fetched_at END, "
               f"source=CASE WHEN {win} THEN excluded.source ELSE {t}.source END")
    params = [(k, d, float(v), source, now, note) for k, d, v in rows]
    if db.dialect == "mysql":                           # 백필은 수천 행 — 500행씩 한 문장으로
        for i in range(0, len(params), 500):
            chunk = params[i:i + 500]
            db.execute(sql.replace("{values}", ", ".join(["(%s, %s, %s, %s, %s, %s)"] * len(chunk))),
                       tuple(x for p in chunk for x in p))
    else:
        for p in params:
            db.execute(sql, p)
    return len(rows)


def load_series(db: DB, keys, since: str) -> dict:
    """{key: [{'d', 'v', 'source', 'note'}...]} 날짜 오름차순. 없는 키는 빈 목록."""
    keys = list(keys)
    out = {k: [] for k in keys}
    if not keys:
        return out
    marks = ", ".join(["%s"] * len(keys))
    for r in db.fetchall(f"SELECT series_key, obs_date, value, source, note FROM alert_macro_series "
                         f"WHERE series_key IN ({marks}) AND obs_date >= %s ORDER BY obs_date", (*keys, since)):
        out[r["series_key"]].append({"d": r["obs_date"], "v": float(r["value"]), "source": r["source"], "note": r["note"]})
    return out


def delete_point(db: DB, key: str, obs_date: str):
    db.execute("DELETE FROM alert_macro_series WHERE series_key = %s AND obs_date = %s AND source = 'manual'", (key, obs_date))


def series_stats(db: DB) -> list:
    return db.fetchall("SELECT series_key, COUNT(*) AS n, MIN(obs_date) AS first, MAX(obs_date) AS last, MAX(fetched_at) AS fetched "
                       "FROM alert_macro_series GROUP BY series_key ORDER BY series_key")


# -- 이벤트 ------------------------------------------------------------------------

EVENT_COLS = ("event_date", "time_local", "country", "kind", "title", "importance", "source", "note", "result", "flag")


def upsert_event(db: DB, ev: dict) -> bool:
    """수집한 일정 — 없으면 넣고, 있으면 시각·중요도·메모만 갱신한다(결과·플래그는 job_results 몫이라 건드리지 않는다).

    반환: 새로 넣었거나 내용이 바뀌었으면 True.
    """
    row = db.fetchone("SELECT id, time_local, importance, note, source FROM alert_macro_events "
                      "WHERE kind=%s AND country=%s AND event_date=%s AND title=%s",
                      (ev["kind"], ev["country"], ev["event_date"], ev["title"]))
    if row is None:
        db.execute(f"INSERT INTO alert_macro_events ({', '.join(EVENT_COLS)}, updated_at) "
                   f"VALUES ({', '.join(['%s'] * (len(EVENT_COLS) + 1))})", (*[ev.get(c) for c in EVENT_COLS], _now()))
        return True
    same = (row["time_local"] == ev.get("time_local") and int(row["importance"]) == int(ev.get("importance", 2))
            and row["note"] == ev.get("note") and row["source"] == ev.get("source"))
    if same:
        return False
    db.execute("UPDATE alert_macro_events SET time_local=%s, importance=%s, note=%s, source=%s, updated_at=%s WHERE id=%s",
               (ev.get("time_local"), ev.get("importance", 2), ev.get("note"), ev.get("source"), _now(), row["id"]))
    return True


def set_event_result(db: DB, event_id: int, result: str, flag: str):
    db.execute("UPDATE alert_macro_events SET result=%s, flag=%s, updated_at=%s WHERE id=%s", (result, flag, _now(), event_id))


def drop_stale_seed_events(db: DB) -> int:
    """수집기가 생기기 전의 시드 실적 일정 — Yahoo 가 채우는 행과 제목이 달라 한 번 지운다."""
    cur = db.execute("DELETE FROM alert_macro_events WHERE source = 'seed' AND kind = 'EARNINGS'")
    return getattr(cur, "rowcount", 0) or 0


def get_event(db: DB, event_id: int):
    return db.fetchone("SELECT * FROM alert_macro_events WHERE id = %s", (event_id,))


def list_events(db: DB, start: str = None, end: str = None) -> list:
    where, params = [], []
    if start:
        where.append("event_date >= %s")
        params.append(start)
    if end:
        where.append("event_date <= %s")
        params.append(end)
    sql = "SELECT * FROM alert_macro_events" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY event_date, time_local, id"
    return db.fetchall(sql, tuple(params))


# -- 시나리오 ------------------------------------------------------------------------

SCENARIO_COLS = ("code", "name", "trigger_text", "soxx_low", "soxx_high", "base_prob", "sort")


def seed_scenarios(db: DB, rows: list) -> int:
    if db.fetchone("SELECT code FROM alert_macro_scenarios LIMIT 1"):
        return 0
    for r in rows:
        save_scenario(db, r)
    return len(rows)


def save_scenario(db: DB, r: dict):
    sql = {
        "mysql": f"INSERT INTO alert_macro_scenarios ({', '.join(SCENARIO_COLS)}, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) AS new "
                 "ON DUPLICATE KEY UPDATE name=new.name, trigger_text=new.trigger_text, soxx_low=new.soxx_low, "
                 "soxx_high=new.soxx_high, base_prob=new.base_prob, sort=new.sort, updated_at=new.updated_at",
        "sqlite": f"INSERT INTO alert_macro_scenarios ({', '.join(SCENARIO_COLS)}, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                  "ON CONFLICT(code) DO UPDATE SET name=excluded.name, trigger_text=excluded.trigger_text, soxx_low=excluded.soxx_low, "
                  "soxx_high=excluded.soxx_high, base_prob=excluded.base_prob, sort=excluded.sort, updated_at=excluded.updated_at",
    }[db.dialect]
    db.execute(sql, (*[r[c] for c in SCENARIO_COLS], _now()))


def list_scenarios(db: DB) -> list:
    return [{**r, "soxx_low": float(r["soxx_low"]), "soxx_high": float(r["soxx_high"]), "base_prob": float(r["base_prob"])}
            for r in db.fetchall("SELECT * FROM alert_macro_scenarios ORDER BY sort, code")]


def log_scenarios(db: DB, log_date: str, base: dict, adjusted: dict):
    sql = {
        "mysql": "INSERT INTO alert_macro_scenario_log (log_date, base, adjusted, created_at) VALUES (%s, %s, %s, %s) AS new "
                 "ON DUPLICATE KEY UPDATE base=new.base, adjusted=new.adjusted, created_at=new.created_at",
        "sqlite": "INSERT INTO alert_macro_scenario_log (log_date, base, adjusted, created_at) VALUES (%s, %s, %s, %s) "
                  "ON CONFLICT(log_date) DO UPDATE SET base=excluded.base, adjusted=excluded.adjusted, created_at=excluded.created_at",
    }[db.dialect]
    db.execute(sql, (log_date, json.dumps(base), json.dumps(adjusted), _now()))


def scenario_history(db: DB, limit: int = 120) -> list:
    rows = db.fetchall("SELECT log_date, adjusted FROM alert_macro_scenario_log ORDER BY log_date DESC LIMIT %s", (limit,))
    return [{"d": r["log_date"], "p": json.loads(r["adjusted"])} for r in reversed(rows)]


# -- 수집 작업 상태 ---------------------------------------------------------------------

def load_bands(db: DB) -> dict:
    """연말 시나리오 구간 스냅샷. 비어 있으면 {} — 워커가 다음 사이클에 새로 자른다."""
    row = db.fetchone("SELECT v FROM alert_settings WHERE k = 'macro_bands'")
    try:
        return json.loads(row["v"]) if row and row["v"] else {}
    except (ValueError, TypeError):
        return {}


def save_bands(db: DB, bands: dict):
    from ..db import set_setting
    set_setting(db, "macro_bands", json.dumps(bands, ensure_ascii=False))


def load_jobs(db: DB) -> dict:
    row = db.fetchone("SELECT v FROM alert_settings WHERE k = 'macro_jobs'")
    try:
        return json.loads(row["v"]) if row else {}
    except ValueError:
        return {}


def save_jobs(db: DB, jobs: dict):
    from ..db import set_setting
    set_setting(db, "macro_jobs", json.dumps(jobs, ensure_ascii=False))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
