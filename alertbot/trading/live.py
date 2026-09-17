"""계정별 live 실행기 묶음 — AUTOTRADE_MODE=live 일 때만 엔진이 만든다.

백오피스에서 바꾼 계정·키·스위치를 워커 재시작 없이 반영한다: 사이클마다 accounts_version 을 보고 바뀌었으면
토스 live 스위치가 켜진 활성 계정마다 그 계정 키로 실행기를 (다시) 만든다. 키가 같으면 기존 실행기를 두고 설정(금액 배율)만 갈아 끼운다.
계좌 연결에 실패한 계정은 그 계정 텔레그램으로 알리고, RETRY_SEC 뒤에 다시 시도한다 (허용 IP 등록 같은 DB 밖의 수정).
"""

import logging
import time

from .. import accounts, db
from ..models import Signal
from ..notify import account_channels
from ..notify.dispatcher import Dispatcher
from ..toss_client import TossReadOnlyClient
from .broker import TossOrderClient
from .executor import Executor

log = logging.getLogger("scalper")

RETRY_SEC = 600


def build_executor(store, account: dict):
    """계정 하나의 live 실행기. 계좌를 못 찾으면 그 계정에 알리고 None."""
    notifier = Dispatcher(account_channels(account), record=lambda s, r: db.log_signal(store, s, r))
    toss = account["toss"]
    cli = TossReadOnlyClient(toss["client_id"], toss["client_secret"])
    try:
        ok, why = cli.load_account(), "BROKERAGE 계좌를 찾지 못했다"
    except (Exception, SystemExit) as e:               # 토큰 실패(허용 IP 403 등)는 SystemExit 로 올라온다
        ok, why = False, str(e)
    if not ok:
        log.warning("계정 %s 토스 live 준비 실패: %s", account["email"], why)
        notifier.send(Signal("AUTOTRADE_DISABLED", "⛔ live 준비 실패", "시스템",
                             f"토스 계좌 연결 실패 — {why}\n키·허용 IP(서버 공인 IP)를 확인한다. {RETRY_SEC // 60}분 뒤 다시 시도한다",
                             account_id=account["id"]), force=True)
        return None
    return Executor(store, TossOrderClient(cli), "live", notifier, account=account)


class LiveExecutors:
    def __init__(self, store, build=build_executor, clock=time.monotonic):
        self.store = store
        self.build = build
        self.clock = clock
        self.hours = None               # 엔진이 넣어 준다 (정규장 판정)
        self.version = None
        self.by_account = {}            # account_id -> (키 서명, Executor)
        self.failed = {}                # account_id -> 실패 시각 (clock)

    @property
    def executors(self) -> list:
        return [ex for _, ex in self.by_account.values()]

    def refresh(self):
        """바뀐 게 없고 재시도할 실패도 없으면 아무것도 안 한다. DB 오류면 기존 실행기를 그대로 둔다."""
        now = self.clock()
        retry = any(now - t >= RETRY_SEC for t in self.failed.values())
        try:
            pending = {o["account_id"] for o in db.recent_orders(self.store, 500)
                       if o["mode"] == "live" and o.get("account_id") is not None and o["status"] in ("sent", "open")}
            version = f"{accounts.accounts_version(self.store)}|{sorted(pending)}"
            if version == self.version and not retry:
                return
            items = [a for a in accounts.live_accounts(self.store, include_ids=pending)
                     if (a["toss_live"] and a["active"]) or a["id"] in pending]
        except Exception as e:
            log.warning("live 계정 조회 실패 — 기존 실행기 유지: %s", e)
            return
        changed, self.version = version != self.version, version
        keep = set()
        for acc in items:
            if acc["error"] or not acc["toss"] or not acc["telegram"]:
                log.warning("계정 %s live 건너뜀: %s", acc["email"], acc["error"] or "토스·텔레그램 키가 없다")
                continue
            sign = (acc["toss"]["client_id"], acc["toss"]["client_secret"], acc["telegram"]["bot_token"], acc["telegram"]["chat_id"])
            cur = self.by_account.get(acc["id"])
            if cur and cur[0] == sign:
                cur[1].account = acc                    # 금액 배율 등 설정만 갱신
                keep.add(acc["id"])
                continue
            if not changed and now - self.failed.get(acc["id"], now - RETRY_SEC) < RETRY_SEC:
                continue                                # 설정은 그대로고 재시도 시각 전
            ex = self.build(self.store, acc)
            if ex is None:
                self.failed[acc["id"]] = now
                continue
            self.failed.pop(acc["id"], None)
            ex.hours = self.hours
            self.by_account[acc["id"]] = (sign, ex)
            keep.add(acc["id"])
            log.info("계정 %s 토스 live 실행기 준비", acc["email"])
        for account_id in set(self.by_account) - keep:
            log.info("계정 %s 토스 live 실행기 제거 (스위치 OFF·키 변경·계정 중지, 미결 주문 없음)", account_id)
            del self.by_account[account_id]
        self.failed = {k: v for k, v in self.failed.items() if k in {a["id"] for a in items}}
