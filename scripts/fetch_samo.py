# -*- coding: utf-8 -*-
"""
Samo-Flow-Korea : KRX 투자자구분 '사모' 종목별 순매수 수집기
- 출처: KRX 정보데이터시스템 (pykrx 경유, KRX 회원 로그인 필요)
- 필요 환경변수: KRX_ID, KRX_PW  (GitHub Secrets)
- 산출물: data/daily/YYYYMMDD.json (일별 원자료), data/latest.json (사이트용 집계)
"""
import os
import sys
import json
import time
import datetime as dt
from pathlib import Path

from pykrx import stock

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "data" / "daily"
OUT_FILE = ROOT / "data" / "latest.json"
META_FILE = ROOT / "data" / "meta.json"

KST = dt.timezone(dt.timedelta(hours=9))
MARKETS = ["KOSPI", "KOSDAQ"]
INVESTOR = "사모"
N_DAYS = 20            # 누적 최대 거래일 수
LOOKBACK_CAL = 45      # 최대 탐색 달력일
MAX_FETCH = 30         # 1회 실행 시 최대 신규 조회 일수
SLEEP = float(os.getenv("KRX_SLEEP","1.2"))            # KRX 차단 방지 지연(초)
READY_HOUR = 18        # 이 시각(KST) 이전 조회분은 잠정치로 보고 재조회


def now_kst():
    return dt.datetime.now(KST)


def find_col(df, key):
    if key in df.columns:
        return key
    for c in df.columns:
        if key in str(c):
            return c
    raise KeyError(f"column '{key}' not found: {list(df.columns)}")


def load_json(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(p, obj):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def fetch_day(d):
    """d='YYYYMMDD'. 반환: {market: {ticker: [name, net_amt, net_vol, buy_amt, sell_amt, close, chg, tv]}} 또는 None(휴장/미집계)"""
    result = {}
    for m in MARKETS:
        net = stock.get_market_net_purchases_of_equities(d, d, m, INVESTOR)
        time.sleep(SLEEP)
        if net is None or net.empty:
            return None
        c_name = find_col(net, "종목명")
        c_na = find_col(net, "순매수거래대금")
        c_nv = find_col(net, "순매수거래량")
        c_ba = find_col(net, "매수거래대금")
        c_sa = find_col(net, "매도거래대금")
        if c_ba == c_na:  # '매수거래대금'이 '순매수거래대금'에 먼저 걸리는 경우 방지
            c_ba = [c for c in net.columns if str(c) == "매수거래대금"][0]
        if float(net[c_ba].abs().sum()) + float(net[c_sa].abs().sum()) == 0:
            return None  # 집계 전(0으로 채워진 응답)

        try:
            px = stock.get_market_ohlcv(d, market=m)
            time.sleep(SLEEP)
        except Exception:
            px = None

        rows = {}
        for t, r in net.iterrows():
            close = chg = tv = None
            if px is not None and not px.empty and t in px.index:
                p = px.loc[t]
                close = int(p.get("종가", 0) or 0)
                chg = round(float(p.get("등락률", 0) or 0), 2)
                tv = int(p.get("거래대금", 0) or 0)
            rows[str(t)] = [
                str(r[c_name]),
                int(r[c_na]),
                int(r[c_nv]),
                int(r[c_ba]),
                int(r[c_sa]),
                close,
                chg,
                tv,
            ]
        result[m] = rows
    return result


def collect_days():
    meta = load_json(META_FILE, {"holidays": []})
    holidays = set(meta.get("holidays", []))
    now = now_kst()
    today = now.date()

    dates, fetched = [], 0
    for back in range(LOOKBACK_CAL):
        day = today - dt.timedelta(days=back)
        if day.weekday() >= 5:
            continue
        d = day.strftime("%Y%m%d")
        if d in holidays:
            continue
        if day == today and now.hour < 16:
            continue  # 장 마감 전

        f = DAILY_DIR / f"{d}.json"
        cached = load_json(f, None)
        need = cached is None
        if cached is not None:
            fa = dt.datetime.fromisoformat(cached["fetched_at"])
            ready_at = dt.datetime.combine(day, dt.time(READY_HOUR, 0), KST)
            if fa < ready_at:
                need = True  # 잠정 조회분 → 재조회

        if need and fetched < MAX_FETCH:
            print(f"[fetch] {d}")
            try:
                data = fetch_day(d)
            except Exception as e:
                print(f"[error] {d}: {e}")
                if cached is None:
                    raise
                data = None
            fetched += 1
            if data is None:
                if day < today and cached is None:
                    holidays.add(d)
                    print(f"[holiday/empty] {d}")
                if cached is None:
                    continue
            else:
                cached = {"date": d, "fetched_at": now.isoformat(), "markets": data}
                save_json(f, cached)

        if cached is not None:
            dates.append(d)
        if len(dates) >= N_DAYS:
            break

    meta["holidays"] = sorted(holidays)[-200:]
    save_json(META_FILE, meta)

    # 창 밖 파일 정리
    keep = set(dates)
    for p in DAILY_DIR.glob("*.json"):
        if p.stem not in keep and p.stem < (min(dates) if dates else "0"):
            p.unlink()
    return sorted(dates, reverse=True)


def aggregate(dates):
    days = {d: load_json(DAILY_DIR / f"{d}.json", None) for d in dates}
    out = {
        "investor": INVESTOR,
        "asof": dates[0],
        "generated_at": now_kst().isoformat(timespec="minutes"),
        "days": list(reversed(dates)),
        "markets": {},
    }
    for m in MARKETS:
        per_day = [days[d]["markets"].get(m, {}) for d in dates]  # 최신순
        tickers = set()
        for pd_ in per_day:
            tickers.update(pd_.keys())
        rows = []
        for t in tickers:
            seq = [pd_.get(t) for pd_ in per_day]
            na = [s[1] if s else 0 for s in seq]
            nv = [s[2] if s else 0 for s in seq]
            if not any(na) and not any(nv):
                continue
            latest = next(s for s in seq if s)
            d0 = seq[0]
            streak = 0
            if na[0] > 0:
                for v in na:
                    if v > 0: streak += 1
                    else: break
            elif na[0] < 0:
                for v in na:
                    if v < 0: streak -= 1
                    else: break
            rows.append([
                t, latest[0],
                na[0], sum(na[:5]), sum(na),
                nv[0], sum(nv[:5]), sum(nv),
                streak,
                d0[5] if d0 else None,
                d0[6] if d0 else None,
                d0[7] if d0 else None,
                d0[3] if d0 else 0,
                d0[4] if d0 else 0,
                list(reversed(na)),
            ])
        totals = [sum(v[1] for v in pd_.values()) for pd_ in reversed(per_day)]
        out["markets"][m] = {"totals": totals, "rows": rows}
    out["columns"] = ["code", "name", "na1", "na5", "na20", "nv1", "nv5", "nv20",
                      "streak", "close", "chg", "tv1", "buy1", "sell1", "hist"]
    return out


def main():
    if not os.getenv("KRX_ID") or not os.getenv("KRX_PW"):
        print("[warn] KRX_ID / KRX_PW 환경변수가 없습니다. GitHub Secrets 등록을 확인하세요.")
    dates = collect_days()
    if not dates:
        print("[error] 수집된 거래일이 없습니다.")
        sys.exit(1)
    save_json(OUT_FILE, aggregate(dates))
    print(f"[done] asof={dates[0]} days={len(dates)}")


if __name__ == "__main__":
    main()
