# -*- coding: utf-8 -*-
"""
Samo-Flow-Korea : KRX 투자자구분 '사모' 종목별 순매수 수집기
- 출처: KRX 정보데이터시스템 (pykrx 경유, KRX 회원 로그인 필요)
- 필요 환경변수: KRX_ID, KRX_PW  (GitHub Secrets)
- 산출물: data/daily/YYYYMMDD.json (일별 원자료), data/latest.json (사이트용 집계)

- 안전장치 (2026-11-27 수정: KRX 로그인 실패가 '휴장일'로 기록되던 문제)
  * 시작할 때 KRX 로그인 세션을 먼저 확인한다. 실패하면 아무 데이터도 쓰지 않고
    종료코드 2로 끝낸다. (로그인 실패 → 빈 응답 → 휴장일 기록 → 그 날짜 영구 누락 방지)
  * 휴장일은 KRX 거래일 캘린더로만 판정한다. 캘린더를 받지 못하면 휴장일을 추가하지 않는다.
  * 기대 거래일(가장 최근 거래일) 데이터를 받지 못하면 종료코드 3으로 끝내,
    워크플로가 조용히 성공한 것처럼 보이지 않게 한다. (커밋 단계도 실행되지 않는다)
  * 직전에 공개한 latest.json 보다 기준일(asof)이 뒤로 가지 않도록 막는다.
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
SLEEP = float(os.getenv("KRX_SLEEP", "1.2"))            # KRX 차단 방지 지연(초)
READY_HOUR = 18        # 이 시각(KST) 이전 조회분은 잠정치로 보고 재조회
RETRY = 2              # 하루치 조회 실패 시 재시도 횟수

EXIT_AUTH = 2          # 자격 증명/로그인 문제
EXIT_DATA = 3          # 데이터 수집 실패


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


# ───────────────────────── KRX 로그인·거래일 ─────────────────────────
def krx_login_reason(login_id, login_pw):
    """로그인 실패 시 KRX 가 돌려준 사유(오류 코드/메시지)를 읽어 출력한다.

    pykrx 는 실패를 False 로만 돌려줘 원인을 알 수 없다. 아이디 미존재(CD005),
    비밀번호 오류, 비밀번호 변경 필요(CD010) 등을 구분해 로그에 남긴다.
    """
    try:
        import requests
        from pykrx.website.comm import auth as krx_auth
    except Exception as e:
        print(f"[auth] 사유 조회 준비 실패: {e}")
        return
    hints = {
        "CD005": "KRX 에 없는 아이디입니다. KRX_ID 값을 확인하세요.",
        "CD010": "비밀번호 변경이 필요합니다. data.krx.co.kr 에서 변경하세요.",
        "CD011": "이미 다른 곳에서 로그인 중입니다(중복 로그인).",
    }
    try:
        s = requests.Session()
        krx_auth.warmup_krx_session(s)
        payload = {"mbrNm": "", "telNo": "", "di": "", "certType": "",
                   "mbrId": login_id, "pw": login_pw}
        r = s.post(krx_auth.LOGIN_URL, data=payload,
                   headers={"User-Agent": krx_auth.USER_AGENT, "Referer": krx_auth.LOGIN_PAGE},
                   timeout=15)
        data = r.json()
        code = str(data.get("_error_code", ""))
        msg = str(data.get("_error_message", ""))
        print(f"[auth] KRX 응답 코드: {code or '(없음)'} / 메시지: {msg or '(없음)'}")
        if code in hints:
            print(f"[auth] {hints[code]}")
        # 아이디는 존재하는데 비밀번호만 다른 경우
        if code and code not in hints:
            print("[auth] 아이디는 인식되었고 비밀번호가 맞지 않거나 다른 사유로 거부되었습니다.")
    except Exception as e:
        print(f"[auth] 사유 조회 실패: {type(e).__name__}: {e}")


def krx_session():
    """pykrx 가 만든 KRX 로그인 세션을 돌려준다. 로그인 실패면 None.

    로그인 개념이 없는 옛 pykrx 버전이면 'not_required' 를 돌려준다.
    """
    try:
        from pykrx.website.comm import auth as krx_auth
    except ImportError:
        print("[auth] pykrx 에 로그인 모듈이 없습니다 (로그인 불필요 버전으로 간주)")
        return "not_required"
    try:
        return krx_auth.get_auth_session()
    except Exception as e:
        print(f"[auth] 세션 확인 중 오류: {e}")
        return None


def krx_trading_days(start, end):
    """KRX 거래일 집합(YYYYMMDD). 조회에 실패하거나 결과가 비면 None."""
    try:
        days = stock.get_previous_business_days(
            fromdate=start.strftime("%Y%m%d"), todate=end.strftime("%Y%m%d")
        )
    except Exception as e:
        print(f"[calendar] 거래일 캘린더 조회 실패: {e}")
        return None
    cal = {d.strftime("%Y%m%d") for d in (days or [])}
    if not cal:
        print("[calendar] 거래일 캘린더가 비어 있습니다.")
        return None
    return cal


# ───────────────────────── 수집 ─────────────────────────
def fetch_day(d):
    """d='YYYYMMDD'. 반환: {market: {ticker: [name, net_amt, net_vol, buy_amt, sell_amt, close, chg, tv]}} 또는 None(미집계)"""
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
    """일별 원자료 수집.

    반환: {dates, missing, errors, expected, pruned}
      dates   : 사용 가능한 거래일(최신순)
      missing : 거래일인데 데이터가 비어 있는 날짜
      errors  : 오류 메시지
      expected: 가장 최근에 있어야 할 거래일
      pruned  : 휴장일 목록에서 잘못 들어가 있어 제거한 실제 거래일
    """
    meta = load_json(META_FILE, {"holidays": []})
    holidays = set(meta.get("holidays", []))
    errors = []
    now = now_kst()
    today = now.date()
    today_s = today.strftime("%Y%m%d")

    start = today - dt.timedelta(days=LOOKBACK_CAL + 10)
    cal = krx_trading_days(start, today)
    if cal is None:
        # 캘린더 없이 '빈 응답'만으로 휴장일을 판정하면 이번 사고가 재발한다.
        raise RuntimeError(
            "KRX 거래일 캘린더를 받지 못했습니다. 로그인/네트워크 상태를 확인하세요. "
            "(휴장일 판정을 하지 않습니다)"
        )

    # 실제 거래일이 휴장일로 잘못 들어가 있으면 바로잡는다
    pruned = sorted(d for d in holidays if d in cal)
    if pruned:
        print(f"[fix] 휴장일 목록에서 실제 거래일 제거: {', '.join(pruned)}")
        holidays -= set(pruned)

    dates, fetched, missing = [], 0, []
    for back in range(LOOKBACK_CAL):
        day = today - dt.timedelta(days=back)
        if day.weekday() >= 5:
            continue
        d = day.strftime("%Y%m%d")
        if day == today and now.hour < 16:
            continue  # 장 마감 전
        f = DAILY_DIR / f"{d}.json"
        cached = load_json(f, None)

        # 휴장일 판정: KRX 거래일 캘린더 기준 (지난 날짜만)
        if d not in cal:
            if day < today and cached is None:
                if d not in holidays:
                    print(f"[holiday] {d}")
                holidays.add(d)
            continue

        need = cached is None
        if cached is not None:
            fa = dt.datetime.fromisoformat(cached["fetched_at"])
            ready_at = dt.datetime.combine(day, dt.time(READY_HOUR, 0), KST)
            if fa < ready_at:
                need = True  # 잠정 조회분 → 재조회

        if need and fetched < MAX_FETCH:
            fetched += 1
            print(f"[fetch] {d}")
            data = None
            for attempt in range(1, RETRY + 1):
                try:
                    data = fetch_day(d)
                except Exception as e:
                    print(f"[error] {d}: {e} (시도 {attempt}/{RETRY})")
                    errors.append(f"{d}: {e}")
                    data = None
                if data is not None:
                    break
                if attempt < RETRY:
                    time.sleep(SLEEP * 2)
            if data is None:
                if cached is None:
                    missing.append(d)
                    print(f"[missing] {d}: 거래일이지만 사모 순매수 데이터가 비어 있습니다")
                    continue
                print(f"[keep] {d}: 새 데이터를 못 받아 기존 파일을 유지합니다")
            else:
                cached = {"date": d, "fetched_at": now.isoformat(), "markets": data}
                save_json(f, cached)

        if cached is not None:
            dates.append(d)
        if len(dates) >= N_DAYS:
            break

    meta["holidays"] = sorted(holidays)[-200:]
    meta["errors"] = errors[-20:]
    save_json(META_FILE, meta)

    # 창 밖 파일 정리
    keep = set(dates)
    for p in DAILY_DIR.glob("*.json"):
        if p.stem not in keep and p.stem < (min(dates) if dates else "0"):
            p.unlink()

    # 가장 최근에 있어야 할 거래일
    ready = [d for d in cal if d < today_s or (d == today_s and now.hour >= READY_HOUR)]
    expected = max(ready) if ready else None

    return {
        "dates": sorted(dates, reverse=True),
        "missing": sorted(missing),
        "errors": errors,
        "expected": expected,
        "pruned": pruned,
    }


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
        # 행 순서를 고정한다(집합 순회 순서는 실행마다 달라져 같은 데이터도 매번 커밋이 생김)
        rows.sort(key=lambda r: (-r[2], r[0]))
        totals = [sum(v[1] for v in pd_.values()) for pd_ in reversed(per_day)]
        out["markets"][m] = {"totals": totals, "rows": rows}
    out["columns"] = ["code", "name", "na1", "na5", "na20", "nv1", "nv5", "nv20",
                      "streak", "close", "chg", "tv1", "buy1", "sell1", "hist"]
    return out


def same_data(a, b):
    """generated_at 만 다른 동일 데이터인지"""
    def strip(o):
        return {k: v for k, v in o.items() if k != "generated_at"}
    return isinstance(a, dict) and strip(a) == strip(b)


def main():
    # 붙여넣기로 딸려온 앞뒤 공백·개행 제거 (그대로면 KRX 가 비밀번호 불일치로 거부한다)
    for k in ("KRX_ID", "KRX_PW"):
        v = os.getenv(k)
        if v is not None and v != v.strip():
            print(f"[auth] {k} 값 앞뒤 공백/개행을 제거했습니다.")
            os.environ[k] = v.strip()

    if not (os.getenv("KRX_ID") and os.getenv("KRX_PW")):
        print("[fatal] KRX_ID / KRX_PW 가 없습니다. GitHub 저장소 Settings → Secrets and variables → "
              "Actions 에서 등록하세요.")
        sys.exit(EXIT_AUTH)

    if krx_session() is None:
        krx_login_reason(os.environ["KRX_ID"], os.environ["KRX_PW"])
        print("[fatal] KRX 로그인 실패. GitHub 저장소 Settings → Secrets and variables → Actions 의 "
              "KRX_ID / KRX_PW 값을 확인하세요. (데이터는 갱신하지 않습니다)")
        sys.exit(EXIT_AUTH)

    try:
        res = collect_days()
    except RuntimeError as e:
        print(f"[fatal] {e}")
        sys.exit(EXIT_DATA)

    dates, expected = res["dates"], res["expected"]
    if not dates:
        print("[fatal] 수집된 거래일이 없습니다.")
        sys.exit(EXIT_DATA)

    if expected and expected not in dates:
        # 기대 거래일이 빠진 채로 공개 데이터를 다시 쓰지 않는다.
        print(f"[fatal] 기대 거래일 {expected} 데이터를 받지 못했습니다 (asof={dates[0]}). "
              "공개 데이터는 그대로 두고 종료합니다.")
        for m in res["missing"]:
            print(f"[fatal] 빈 응답: {m}")
        sys.exit(EXIT_DATA)

    old = load_json(OUT_FILE, None)
    old_asof = (old or {}).get("asof")
    out = aggregate(dates)
    if old_asof and out["asof"] < old_asof:
        print(f"[fatal] 기준일이 {old_asof} → {out['asof']} 로 뒤로 갑니다. 갱신을 중단합니다.")
        sys.exit(EXIT_DATA)

    if same_data(old, out):
        print(f"[skip] 새로 받은 데이터 없음 (asof={out['asof']}, days={len(dates)})")
    else:
        save_json(OUT_FILE, out)
        print(f"[done] asof={out['asof']} days={len(dates)}")

    if res["missing"]:
        print("[fail] 데이터가 빈 거래일: " + ", ".join(res["missing"]))
        sys.exit(EXIT_DATA)
    if res["errors"]:
        print(f"[warn] 이번 실행 오류 {len(res['errors'])}건 (마지막: {res['errors'][-1]})")


if __name__ == "__main__":
    main()
