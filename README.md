# Samo-Flow-Korea (사모펀드 수급 CHECK)

KRX 투자자구분 '사모' 종목별 순매수·순매도 순위 (KOSPI·KOSDAQ, 1일·5일·20일 누적)

- 데이터: KRX 정보데이터시스템 (pykrx 경유, KRX 회원 로그인 필요)
- 갱신: 평일 18:30 KST, 익일 08:00 KST 재확인 (GitHub Actions)
- 필요 Secrets: KRX_ID, KRX_PW
- 실패 시 동작: KRX 로그인·수집에 실패하면 그 날짜를 휴장일로 기록하지 않고
  워크플로를 실패로 끝냅니다(종료코드 2=로그인, 3=데이터). 데이터는 갱신·커밋되지 않습니다.
- 주소: https://bluelagoon1222.github.io/Samo-Flow-Korea/
