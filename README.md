# 카셰어링 수요/인프라 지도 (`carsharing-demand-map`)

> 쏘카 사업팀의 **잠재 수요 / 운영 자산 / 충전 인프라 / 미진출·재진입 지역**을 한 화면에서 보는 Leaflet 기반 인터랙티브 지도. BigQuery + 환경부 충전기 API로 데이터를 수집하고, 정적 HTML 한 파일로 직렬화해서 GitHub Pages에 배포한다.

- **Live**: <https://dustin2233.github.io/carsharing-demand-map/>
- **Repository**: <https://github.com/dustin2233/carsharing-demand-map>
- **Owner**: Dustin (`dustin@socar.kr`), 경기강원사업팀

---

## 1. 이 지도가 보여주는 것

각 사업팀(권역)별로 한 페이지(`<team>/index.html`)가 생성된다. 한 페이지 안에 다음과 같은 기능들이 모두 들어 있다:

| 분류 | 기능 | 절 |
|---|---|---|
| 수요 히트맵 | 앱 접속 / 예약 생성 / 부름 호출 | [1.1](#11-수요-레이어-히트맵) |
| 공급·자산 | 운영 존 / 그린카 존 / 폐쇄 존 / EV 운영 존 / 충전보장형 | [1.2](#12-공급--자산-레이어) |
| 충전 인프라 | 신규 EV존 후보 충전소 + **충전 혼잡도 0~23시 차트** | [1.3](#13-충전-인프라-레이어) [1.7](#17-충전-혼잡도-시각화-compute_ev_congestion--popup-chart) |
| 분석 | 미진출 지역 / 재진입 추천 / Market Share | [1.4](#14-분석-레이어) |
| 검색 | 주소 검색 / 존 검색 | [1.5](#15-검색--인터랙션-도구) |
| 측정 | 거리 측정 / 반경 측정 | [1.5](#15-검색--인터랙션-도구) |
| 시뮬레이션 | **존 개설 시뮬레이션** (BQ 실시간 + LLM 평가 + GPM 5시나리오) | [1.6](#16-존-개설-시뮬레이션-simulate_zone--evaluate_simulation_llm) |
| 도착지 분포 | D2D 부름 도착지 점도 | [1.5](#15-검색--인터랙션-도구) |
| 운영 | 사이드바 백그라운드 업데이트 (수요/존) | [1.8](#18-사이드바-백그라운드-업데이트) |
| 인증 | 비밀번호 쿠키 (로컬·ngrok만) | [1.5](#15-검색--인터랙션-도구) |

이하 상세.

### 1.0 사이드바 토글 14개 레이어

다음 레이어를 사이드바에서 켜고 끌 수 있다.

### 1.1 수요 레이어 (히트맵)
| 레이어 | 의미 | 색상 |
|---|---|---|
| **앱 접속** | 최근 3개월 앱 접속 좌표 히트맵 (위치 권한 ON 사용자) | `#fb8c00` |
| **예약 생성** | 최근 3개월 예약 시작 위치 히트맵 | `#7e57c2` |
| **부름 호출** | 최근 3개월 부름(D2D) 호출 위치 (HAVING ≥ 12회) | `#00bcd4` |

격자 수는 존 수에 비례해 동적으로 결정 (`_calc_grid_limit`): 존당 약 3~4 도트, 최소 500 / 최대 3,000, 100단위 반올림.

### 1.2 공급 / 자산 레이어
| 레이어 | 의미 | 색상 |
|---|---|---|
| **운영 존** | 현재 쏘카 운영존(차량 1대 이상). 차량 수, 실적, 주차 계약, 사전가동, 예약 타임라인 popup | `#27ae60` |
| **그린카 존** | 그린카가 운영 중인 존 (경쟁 정보) | `#ef5350` |
| **폐쇄 존** | 과거 운영 후 폐쇄된 존 | `#616161` |
| **EV 운영 존** | 전기차 배치 존 (`car_name LIKE '%전기차%'` 또는 가상존) | `#1565c0` |
| **충전보장형** | "충전보장 EV" 가상존만 필터링 | `#0d47a1` |

### 1.3 충전 인프라 레이어
| 레이어 | 의미 | 색상 |
|---|---|---|
| **충전 인프라** | 환경부 충전기 API에서 가져온 충전소 중 **운영 존 미매칭 = 신규 EV존 후보** (100m 이내 운영존 있으면 제외, 충전기 3기 미만 제외, 에버온/차지비 로밍 가능만) | `#42a5f5` |

서브 필터: `전체 / 에버온(일반) / 차지비(미니EV)`

### 1.4 분석 레이어
| 레이어 | 의미 | 색상 |
|---|---|---|
| **미진출 지역 분석 (Gap)** | 수요는 있는데 운영존이 없는 지역 (`compute_gaps`) | `#8e44ad` |
| **재진입 추천구역** | 과거 폐쇄존 중 주변 수요가 회복된 곳 | `#ec407a` |
| **Market Share** | 권역별 쏘카 점유율 비교 | `#ff7043` |

### 1.5 검색 & 인터랙션 도구
- **주소 검색 / 존 검색** (사이드바 상단 탭): `searchModeAddr`로 카카오 주소→좌표, `searchModeZone`로 존명/존ID 즉시 검색 → 드롭다운 결과 클릭 시 지도 이동.
- **거리/반경 측정** (`measureLayer`): 두 가지 모드.
  - **거리**: 클릭 누적으로 폴리라인, 각 세그먼트 + 총거리를 카드로 표시.
  - **반경**: 중심 클릭 후 드래그로 원형 반경 + 총반경 카드.
- **D2D 도착지 분포** (`d2dDestLayer`): 존 클릭 → "부름 도착지" 버튼 → 해당 존의 최근 부름 도착 좌표 점도. "운영 존 보기" 토글로 같이 비교.
- **사이드바 리스트 패널**:
  - **재진입 패널** (`reentryPanel`): 권역 region2 필터, 클릭 시 지도 이동 + popup.
  - **Market Share 패널** (`marketSharePanel`): region2별 쏘카/그린카 점유 표.
  - **Gap 패널**: 미진출 지역 리스트 + 클릭 이동.
- **운영 존 popup** (자기완결형 카드): 존 ID, 차량 수, 매출/GP·가동률, 사전가동률, demand_grade, 주차 계약(공급자·정산방식·대당단가), 예약 타임라인(요일×시간 혼잡 히트맵), EV 차량 수 / 충전소 상세(이름·기수·동일현장 여부·혼잡도) 등.
- **인증**: `server.py`로 로컬 호스팅 시 비밀번호 쿠키(`socar_auth`, 24h). localhost는 우회. GitHub Pages는 공개.

### 1.6 존 개설 시뮬레이션 (`simulate_zone` + `evaluate_simulation_llm`)

지도 위 **우클릭(또는 `존 개설 시뮬레이션` 버튼)** → 임의 좌표에서 가상 존을 열었을 때 어떤 결과가 나올지 실시간 추정.

진입 방식:
- 지도 임의 좌표 **우클릭** → "여기서 시뮬레이션" 컨텍스트
- 또는 사이드바 우측 하단 `simCtxBtn` ("존 개설 시뮬레이션") 클릭

산출 흐름:
1. **반경 500m bbox** 안에서 BQ 실시간 쿼리 5개 동시 실행
   - 앱 접속 / 예약 생성 / 부름 호출 / region3·region2 전환율 / 인근 벤치마크 존(반경 내 기존 존)
2. **수요 → 예상 예약** 환산: 접속 기반 + 부름 전환 추가분
3. **벤치마크**: 반경 내 기존 존들의 대당매출/대당GP/가동률 평균
4. **카니발리제이션**: 반경 500m 내 기존 존 차량 수만큼 차감
5. **추천 공급대수**: 수요 기반 적정대수 − 카니발 차감
6. **GPM 시뮬레이션**: 단가 −25% / −10% / 기준 / +10% / +25% 5시나리오별 GP 추정
7. **LLM 평가**: GPT 5.2 + Claude 3.5 Sonnet으로 텍스트 결론 (성장 가능성·리스크·권장 차종)
8. **과거 존 이력**: 같은 좌표 근방의 폐쇄존 이력 표시

popup에 표시되는 핵심 지표:
- 위치(region2/region3), 수요 등급, 격자 상위 %
- 주간 접속/예약/부름 수
- 전환율 (region2 내 region3 순위, 평균/최고/최저)
- 예상 주간 예약 = `base_res + dtod_additional`
- 인근 벤치마크 대당매출·대당GP·가동률
- 건당매출, 추정 월매출
- 수요 기반 적정대수, 카니발 차감 후 추천 공급대수
- 최근접 존 + 거리 (km)
- 과거 존 이력 N건
- "산출 기준 상세" 펼치기 (모든 가중치·SQL 노출)

**서버 의존**: `POST /api/simulate` + `POST /api/simulate-eval` (server.py) → BQ 실시간 쿼리 + LLM 호출. **GitHub Pages 배포본은 ngrok 터널(7.6) 경유**해서 이 두 API를 호출한다. 터널이 죽으면 시뮬레이션 작동 안 함.

### 1.7 충전 혼잡도 시각화 (`compute_ev_congestion` + popup chart)

각 충전소 popup 안에 **0~23시 시간대별 막대그래프**가 떠 있다. 막대 색 진하기 = 그 시각 충전중인 충전기 / 전체 충전기 비율.

데이터 파이프라인:
1. **`collect_ev_status.py`** (cron 30분 간격) → 환경부 `getChargerInfo` 전체 충전기 상태 폴링
2. `.cache/ev_status_log/YYYY-MM-DD.jsonl` 한 줄 = 한 폴링 (충전소별 charging/total 카운트 + hour + weekday)
3. **`compute_ev_congestion()`** → 누적 jsonl 전부 읽어 충전소별 `[h0%, h1%, …, h23%]` 24개 배열
4. `ev_chargers.json`의 각 station에 `congestion` 필드로 주입
5. HTML JS `evCongestionChart(congestion)` 함수가 popup 안에 막대 차트 렌더

stat 코드 매핑 (`collect_ev_status.py`):
- `1` 통신이상, `2` 충전대기, `3` 충전중, `4` 운영중지, `5` 점검중, `9` 상태미확인
- 혼잡도 = `STAT_CHARGING(3) / total`

수집 일수가 부족하면 라벨에 **"(수집 중)"** 표시, 데이터 없는 시간(`total=0`)은 `-1`로 마킹해서 빈 막대.

### 1.8 사이드바 백그라운드 업데이트

`server.py` 환경에서만 노출되는 버튼 2개:
- **`수요 업데이트`** → `POST /api/update-demand` → BQ에서 접속/예약/부름만 갱신 + HTML 재생성
- **`존/실적 업데이트`** → `POST /api/update-zone` → 존/실적/주차계약/그린카/폐쇄존/충전소/EV 가상존/사전가동 + HTML 재생성

브라우저 떠난 채로 백그라운드에서 진행, 완료 시 `.last_update_demand` / `.last_update_zone` 타임스탬프 갱신, 헤더에 마지막 업데이트 시각 자동 표시.

---

## 2. 지원하는 권역 (`TEAM_CONFIG`)

`update.py`의 `TEAM_CONFIG`에 정의된 6개 권역. 각 권역마다 별도의 캐시·HTML이 생성된다.

| `team_id` | 팀명 | 포함 시도 | 지도 중심 / 줌 |
|---|---|---|---|
| `seoul` | 서울사업팀 | 서울특별시, 인천광역시 | [37.5665, 126.978] z11 |
| `gyeonggi` | 경기강원사업팀 | 경기도, 강원도 | [37.5, 127.8] z8 |
| `chungcheong` | 충청사업팀 | 충북, 충남, 대전, 세종 | [36.5, 127.0] z10 |
| `gyeongbuk` | 경북사업팀 | 경상북도, 대구광역시 | [36.0, 128.6] z10 |
| `gyeongnam` | 부울경사업팀 | 부산, 울산, 경상남도 | [35.2, 129.0] z10 |
| `honam` | 호남사업팀 | 전남, 전북, 광주 | [35.1, 126.9] z10 |

권역 경계는 `korea_provinces.json`(시도 GeoJSON, 17개 시도)을 unary_union으로 합쳐 `prepared polygon`으로 만들어, 좌표가 권역 안인지 빠르게 contains 판정한다(`_get_team_polygon`).

> **메모**: 환경부 API는 강원도를 zcode `51`(강원특별자치도)로 쓰고, geojson은 `'강원도' (code 32)`로 들어가 있어 코드 안에 매핑 테이블이 있다(`EV_CHARGER_ZCODES`, `_REGION_ALIASES`, `name_map`).

---

## 3. 디렉토리 구조

```
carsharing_demand_map/
├── update.py                  # 메인 빌드 스크립트 (BQ 쿼리 + HTML 생성, 5,800줄)
├── server.py                  # 로컬 인증 서버 (port 8080)
├── collect_ev_status.py       # 충전기 상태 수집 (cron 30분)
├── generate_dashboards.py     # 보조 대시보드 생성기
├── korea_provinces.json       # 시도 GeoJSON (행정구역 필터용)
├── index.html                 # 루트 페이지 (단일팀: 해당 팀 지도 / 멀티팀: 랜딩)
├── dashboard.html             # 부가 대시보드 (실적 추이)
├── start_ngrok.sh             # ngrok 듀얼 도메인 터널 (8080 → dustin.ngrok.app + emil-….ngrok-free.dev)
├── .ngrok_url                 # 현재 활성 대시보드 ngrok 도메인 (update.py가 HTML에 임베드)
├── <team_id>/
│   └── index.html             # 권역별 자기완결형 HTML (~20MB, 데이터 embed)
└── .cache/
    ├── ev_status_log/         # 30분 단위 충전기 상태 JSONL
    └── <team_id>/             # 권역별 캐시
        ├── access.json        # 앱 접속 (lat,lng,count)
        ├── reservation.json   # 예약 위치
        ├── dtod.json          # 부름 호출
        ├── zones.json         # 운영존 (lat,lng,name,car_count, …)
        ├── profit.json        # 존별 실적
        ├── parking_contract.json  # 주차 계약·정산 정보
        ├── gcar.json          # 그린카 존
        ├── closed.json        # 폐쇄 존
        ├── socar_supply.json  # 지역별 운영 차량/존 수
        ├── timeline.json      # 예약 타임라인 (혼잡 시간대)
        ├── weekly_trends.json # 주간 접속/예약/공급 추이
        ├── gaps.json          # 미진출 지역
        ├── supply_demand.json # 성장·감소 지역 분석
        ├── advance_util.json  # 사전가동률 + demand_grade
        ├── ev_chargers.json   # 환경부 충전소 (statId 단위 집계)
        ├── ev_car_zones.json  # 존별 EV 차량 수
        ├── ev_virtual_zones.json  # 충전보장 가상존 매핑
        ├── dashboard_metrics.json    # 주간 KPI
        ├── dashboard_region2.json    # region2 주간 KPI
        └── dashboard_region3.json    # region3 주간 KPI
```

HTML은 **자기완결형**(self-contained)이다 — 데이터는 `var accessData = [...]; var zonesData = [...]; ...` 형태로 `<script>` 안에 직렬화돼 들어가고, 외부 데이터 fetch는 없다. 파일 크기가 20MB까지 커지는 이유.

---

## 4. 아키텍처 / 데이터 흐름

```
                              ┌────────────────────────┐
                              │   BigQuery (socar-data)│
                              │   (12개 이상 테이블)   │
                              └───────────┬────────────┘
                                          │ bq query
                                          ▼
┌────────────────────┐         ┌────────────────────────┐
│ 환경부 EV API      │  HTTP   │   update.py (Python)   │
│ B552584/EvCharger  ├────────▶│  - query_*()           │
│ (zcode=41,51,…)    │         │  - compute_*()         │
└────────────────────┘         │  - generate_index()    │
                               └───────────┬────────────┘
                                          │  json 저장
                                          ▼
                              ┌────────────────────────┐
                              │ .cache/<team>/*.json   │  ← 캐시 (재빌드 시 재사용)
                              └───────────┬────────────┘
                                          │  json.dumps → <script>에 embed
                                          ▼
                              ┌────────────────────────┐
                              │ <team>/index.html      │
                              │ index.html (root copy) │
                              └───────────┬────────────┘
                                          │ git push
                                          ▼
                              ┌────────────────────────┐
                              │ GitHub Pages / Netlify │
                              └────────────────────────┘
```

빌드 함수는 3개:
- **`main(team_id)`** — BQ에서 처음부터 모두 가져와 풀 빌드 (기본).
- **`update_demand(team_id)`** — 수요(접속/예약/부름)만 갱신, 그 외는 cache 재사용.
- **`update_zone(team_id)`** — 존/실적/충전 인프라만 갱신.
- **`regenerate_from_cache(team_id)`** — BQ 호출 없이 cache만으로 HTML 재생성 (디자인 수정 후 빠르게 확인용).

세 함수 모두 **단일팀이면 `<team>/index.html`을 루트 `index.html`로 복사**한다 (GitHub Pages 루트 stale 방지).

---

## 5. 데이터 소스

### 5.1 BigQuery (프로젝트: `socar-data`)

| 데이터셋 | 테이블 | 용도 |
|---|---|---|
| `socar_server_3` | `GET_MARKERS`, `GET_MARKERS_V2` | 앱 접속(`access`), 부름(`dtod`) 위치 — 위치 권한 ON 이벤트 |
| `soda_store` | `reservation_v2` | 예약 위치, 예약별 region 집계 |
| `tianjin_replica` | `reservation_info`, `reservation_dtod_info`, `car_info`, `carzone_info` | 예약 타임라인, 부름 도착지, 차량/존 메타 |
| `socar_zone` | `zone`, `parking`, `parking_contract`, `parking_settlement_policy`, `parking_policy`, `settlement_type_policy`, `rent_policy`, `parking_provider` | 운영존, 주차 계약/정산/요율 |
| `socar_biz_profit` | `profit_socar_car_daily` | 차량/존별 실적 (매출, GP 등) |
| `socar_biz` | `operation_per_car_daily_v2` | 대당 가동, 사전가동 |
| `greencar` | `gcar_zone_info_log` | 그린카 존 현황 |

**인증**: 로컬에서 `bq` CLI (`gcloud auth login` 또는 Service Account) 사용. SQL은 `run_bq()` → `subprocess.run(['bq', 'query', …])` 형태.

### 5.2 환경부 충전기 API

```
http://apis.data.go.kr/B552584/EvCharger/getChargerInfo
```

- `serviceKey`: 코드 안에 임베드 (공공데이터포털 인증키)
- `zcode`: 시도 코드 (서울 11, 경기 41, 강원 51, 충북 43, 충남 44, 대전 30, 세종 36, 경북 47, 대구 27, 경남 48, 부산 26, 울산 31, 전북 45, 전남 46, 광주 29)
- 페이지당 9,999건, 페이지네이션
- `chgerType`: `01`=DC차데모, `02`=AC완속, `03`~`07` 콤보/3상 → 급속 여부 판정
- `statId` 단위로 집계해서 충전소(역) 단위 fast/slow 카운트
- 좌표 기준 권역 polygon으로 필터

**로밍 매칭 키워드** (충전소 사업자명 부분일치):
- `EVERON_KEYWORDS`: 한국전기차충전, 한국전자금융, NICE인프라, 나이스차저, 현대엔지니어링 등 → 에버온 로밍 가능 (일반 EV)
- `CHARGEV_KEYWORDS`: 차지비 계열 → 미니EV 충전 가능

---

## 6. 핵심 상수 / 룰

| 항목 | 값 | 위치 |
|---|---|---|
| 데이터 기간 | 최근 3개월 | `THREE_MONTHS_AGO` |
| 존-충전소 매칭 반경 | **100m** (haversine) | `match_ev_to_zones(radius_km=0.1)` |
| 부름 호출 표시 임계 | HAVING ≥ 12회 | `query_dtod` |
| 격자 수 (히트맵 도트) | `max(500, min(3000, zone_count × 3))`, 100단위 반올림 | `_calc_grid_limit` |
| 충전 인프라 표시 임계 | 충전기 ≥ 3기 AND (에버온 OR 차지비) AND 운영존 100m 외 | 사이드바 evLayer (update.py 4030~) |
| 시뮬레이션 반경 | 기본 0.5km | `simulate_zone` |

---

## 7. 실행 방법

### 7.1 의존성

```bash
pip3 install shapely
# bq CLI (Google Cloud SDK) 필요
gcloud auth login
gcloud config set project socar-data
```

> Python 3.11+ 권장. `shapely` 외 표준 라이브러리만 사용.

### 7.2 빌드 명령어

```bash
# 전체 권역 풀 빌드
python3 update.py --team all

# 단일 권역 풀 빌드 (기본은 gyeonggi)
python3 update.py --team gyeonggi

# 수요만 갱신 (앱 접속/예약/부름)
python3 update.py --demand --team gyeonggi

# 존/실적/충전 인프라만 갱신
python3 update.py --zone --team gyeonggi

# BQ 호출 없이 cache로 HTML만 재생성
python3 update.py --regen --team gyeonggi
```

`--team all` 또는 인자 생략 시 모든 권역을 순차 빌드하고, 마지막에 `generate_landing_page()`가 루트 `index.html`을 **권역 선택 랜딩 페이지**로 덮어쓴다.

### 7.3 로컬 서버 (`server.py`, port 8080)

```bash
python3 server.py    # http://localhost:8080  (비밀번호 보호, localhost는 우회)
```

정적 서빙 + 비밀번호 쿠키 인증 + 다음 API 엔드포인트를 제공한다.

| Method | Path | 용도 |
|---|---|---|
| POST | `/auth/login` | 비밀번호 검증 → `socar_auth` 쿠키(24h) 발급 |
| POST | `/api/update-demand` | 수요(접속/예약/부름) 백그라운드 갱신 + HTML 재생성 |
| POST | `/api/update-zone` | 존/실적/주차계약/충전소/EV/사전가동 백그라운드 갱신 |
| POST | `/api/update` | `/api/update-demand` 별칭 |
| POST | `/api/simulate` | **존 개설 시뮬레이션** — 좌표 받아 BQ 실시간 집계 (`simulate_zone`) |
| POST | `/api/simulate-eval` | 시뮬레이션 결과를 LLM(GPT+Claude)에 던져 텍스트 평가 |
| POST | `/api/d2d-destinations` | 특정 존의 부름 도착지 좌표 (`query_d2d_destinations`) |
| POST | `/api/dashboard-analyze` | 대시보드 보조 분석 |
| POST | `/api/dashboard-region3` | region3 단위 대시보드 데이터 |
| GET | `/api/status` | 마지막 업데이트 타임스탬프 |
| GET | `/api/ai-config` | LLM 모델/키 설정 |
| GET | `/api/dashboard*` | 대시보드용 GET 엔드포인트들 |

API는 인증 우회로 통과 (브라우저 쿠키 자동 포함). 정적 페이지(`/`, `/gyeonggi/`)는 인증 필수.

### 7.4 충전기 상태 수집 (cron)

```bash
# 30분 간격
*/30 * * * * /usr/bin/python3 /Users/dustin/carsharing_demand_map/collect_ev_status.py
```

`.cache/ev_status_log/YYYY-MM-DD.jsonl`에 충전중/대기 상태가 누적되고, `compute_ev_congestion()`이 충전소별 시간대(0~23시) 혼잡도 배열을 만들어 popup 차트에 사용한다.

### 7.5 배포

GitHub Pages가 `main` 브랜치 루트의 `index.html`을 자동 서빙. Netlify는 push webhook으로 자동 트리거. 별도 빌드 step 없음.

```bash
git add . && git commit -m "update" && git push origin main
```

> **주의 (2026-05-12 학습)**: 빌드 함수 3개 모두 단일팀일 때 루트 `index.html` 복사 로직을 갖고 있어야 한다. 하나만 빠져 있어도 GitHub Pages 루트가 stale해진다.

### 7.6 ngrok 터널 (외부 임시 공개 + 시뮬레이션 API 백엔드)

GitHub Pages는 정적 호스팅이라 시뮬레이션(`POST /simulate`), D2D 도착지(`POST /d2d_destinations`) 같은 **서버 측 API를 호출하지 못한다**. 이 API를 외부에서도 동작시키려고 `server.py`(port 8080)를 **ngrok 터널 2개**로 동시에 노출한다.

```bash
bash start_ngrok.sh
```

| 도메인 | 용도 | 비고 |
|---|---|---|
| `https://dustin.ngrok.app` | 실적 대시보드 (`/dashboard.html`) | 유료/예약 도메인 |
| `https://emil-unemancipated-joya.ngrok-free.dev/gyeonggi/` | 수요/인프라 지도 | ngrok-free.dev |

두 ngrok 프로세스가 같은 8080(`server.py`)으로 들어가고, `server.py`는 **`Host` 헤더로 분기**해서 같은 서버에서 두 화면을 다르게 보여준다 (로고/타이틀/기본 경로 등).

**API base URL 동적 결정** (`update.py`에서 `generate_index`가 HTML에 임베드):
1. 빌드 시 `_read_ngrok_url()`이 `.ngrok_url` 파일을 읽어 현재 활성 도메인을 가져옴.
2. HTML 안 JS는 `location.hostname`을 보고 분기:
   - `localhost` / `127.0.0.1` / `*.ngrok-free.dev` → 같은 origin (relative path)
   - 그 외 (GitHub Pages 등) → 임베드된 ngrok URL로 cross-origin 호출
3. fetch 요청에 `ngrok-skip-browser-warning: true` 헤더를 항상 같이 보내 ngrok 무료 경고 페이지를 우회.

즉 **GitHub Pages 배포본도 ngrok 터널이 살아있어야 시뮬레이션이 동작**한다. 터널이 죽어도 정적 지도(레이어, popup)는 정상 동작.

ngrok 재시작·도메인 교체 시:
1. `start_ngrok.sh` 안의 `DOMAIN_DASH` / `DOMAIN_MAP` 수정
2. `bash start_ngrok.sh` 실행 → `.ngrok_url` 자동 갱신
3. `python3 update.py --regen --team gyeonggi` → 새 도메인이 HTML에 다시 임베드
4. `git push` → GitHub Pages 갱신

---

## 8. 외부 라이브러리 (CDN)

HTML에는 다음 자원이 `<head>` 안에 CDN으로 로드된다 — 별도 패키지 매니저 없음.

- **Leaflet 1.9.4** — 지도 본체
- **Leaflet.heat 0.2.0** — 히트맵
- **Leaflet.markercluster 1.5.3** — 클러스터링
- **Pretendard** (jsDelivr/orioncactus) — 폰트

지도 타일은 OpenStreetMap 기본.

---

## 9. 통합 (Integration) 가이드

다른 작업물에 이 지도를 녹이는 방법은 두 가지.

### 9.1 (간단) iframe / 링크 임베드

```html
<iframe
  src="https://dustin2233.github.io/carsharing-demand-map/gyeonggi/index.html"
  style="width:100%;height:100vh;border:0">
</iframe>
```

또는 ngrok 도메인 직접 사용 (시뮬레이션 API 같은 origin으로 동작):
```
https://emil-unemancipated-joya.ngrok-free.dev/gyeonggi/
```

- 각 권역 페이지는 자기완결형이라 iframe만으로 모든 인터랙션 동작.
- 사이드바 토글, popup, 측정 도구는 정적이라 항상 동작.
- **시뮬레이션·D2D 도착지 API**는 7.6에서 설명한 ngrok 터널이 살아있어야 동작.
- 단점: 페이지 외부에서 지도 상태(선택된 존 등)에 접근 불가, 디자인 통합 어려움, ~20MB 로딩.

### 9.2 (권장) 데이터·로직 재사용

`.cache/<team_id>/*.json`이 모든 데이터의 원본이므로, 동료 작업물이 직접 이 JSON을 읽어 자체 지도로 렌더링하는 것이 가장 유연하다.

가장 자주 쓰일 만한 4개 파일:

| 파일 | 스키마 (key 예시) |
|---|---|
| `zones.json` | `zone_id, zone_name, lat, lng, car_count, region1/2/3, sig_name, …` |
| `access.json` | `lat, lng, access_count` (히트맵 dot) |
| `reservation.json` | `lat, lng, reservation_count` |
| `ev_chargers.json` | `statId, statNm, addr, lat, lng, busiNm, fast, slow, total, everon, chargev, congestion` |

JSON은 GitHub raw URL로 직접 가져오기 가능:
```
https://raw.githubusercontent.com/dustin2233/carsharing-demand-map/main/.cache/gyeonggi/zones.json
```

> **단, `.cache/` 가 어디까지 깃에 커밋돼 있는지는 확인 필요**. 현재 `.gitignore`가 없고 일부 cache는 트래킹되고 있으나, 권역에 따라 누락 파일이 있을 수 있음.

### 9.3 기능별 서버 의존도 매트릭스

iframe / 데이터 재사용 어떤 방식이든, **순수 정적**인 기능과 **백엔드(`server.py` + ngrok 또는 같은 origin)**가 필요한 기능을 구분해서 통합 설계해야 한다.

| 기능 | GitHub Pages만으로? | server.py 필요? |
|---|:---:|:---:|
| 사이드바 14개 레이어 토글 | ✅ | – |
| 운영존 popup (실적·계약·타임라인·사전가동) | ✅ | – |
| 충전소 popup + **충전 혼잡도 차트** | ✅ (cron 누적분이 cache에 들어가 있으면) | – |
| 주소/존 검색 | ✅ | – |
| 거리/반경 측정 | ✅ | – |
| Gap / 재진입 / Market Share 패널 | ✅ | – |
| **존 개설 시뮬레이션** | ❌ | ✅ (BQ + LLM) |
| **D2D 도착지 분포** | ❌ | ✅ (BQ 실시간) |
| 사이드바 **수요/존 업데이트** 버튼 | ❌ | ✅ |
| 마지막 업데이트 시각 표시 | ⚠️ HTML 빌드 시점 고정 | ✅ 실시간 |

> 동료가 본인 작업물에 시뮬레이션·D2D 도착지까지 살리려면 ngrok 터널(7.6) 또는 자체 `server.py` 호스팅이 필요하다.

### 9.4 동일 BQ 쿼리 재사용

스키마·지표 정의를 함께 쓰고 싶다면 `update.py`의 `query_*()` 함수들이 가장 좋은 참조점이다 — 모든 쿼리가 `team_id`만 받아 `_region1_sql()` / polygon 필터 / 3개월 윈도우를 일관되게 적용한다.

핵심 함수 (line 번호 참고용, 2026-05-12 기준):
- `query_access` (192), `query_reservation` (227), `query_dtod` (274)
- `query_zones` (920), `query_zone_profit` (1494), `query_parking_contract` (1019)
- `query_ev_chargers` (1145), `compute_ev_congestion` (1272), `query_ev_virtual_zones` (1347)
- `compute_gaps` (2139), `compute_reentry_zones` (2203), `compute_growth_analysis` (2542)
- `query_dashboard_metrics` (1546), `query_dashboard_by_region` (1780), `query_advance_utilization` (2285)

---

## 10. 알려진 제약 / 주의사항

1. **위치 권한 데이터**: 앱 접속/부름 히트맵은 위치 권한 ON 사용자만 잡힌다 → 절대량보다는 상대 비교용.
2. **자기완결형 HTML 크기**: 권역당 ~20MB. 모바일 3G에서는 로딩이 느리다. 핵심 데이터를 별도 JSON으로 떼어내 lazy load 가능하지만 현재 미구현.
3. **권역 polygon 매칭 vs 행정코드**: 환경부 zcode와 행정안전부 geojson code가 서로 다르다(`강원특별자치도=51` vs `강원도=32`). 매핑은 `name_map`/`_REGION_ALIASES`/`EV_CHARGER_ZCODES`에서 유지.
4. **시뮬레이션은 LLM 호출 (`evaluate_simulation_llm`)** — 외부 API 키 + 비용 발생. GitHub Pages 배포본은 정적이라 시뮬레이션/D2D 도착지 API를 직접 못 띄우고, **로컬 `server.py` + ngrok 터널**(7.6)을 통해 cross-origin으로 호출한다. 터널이 죽으면 시뮬레이션도 죽음.
5. **대시보드 페이지(`dashboard.html`)는 별도 흐름** — `generate_dashboards.py`가 `~/`의 JSON을 읽어 만든다(과거 PoC). 메인 지도와는 데이터 소스가 다를 수 있음.
6. **인증**: GitHub Pages 배포본은 공개. `server.py` 로컬 호스팅 시에만 비밀번호 보호.
7. **API Key 노출**: 환경부 충전기 API 키가 코드에 하드코딩되어 있다 (공공데이터 인증키 — 트래픽 제한만 영향).

---

## 11. 컨택

- **Owner**: Dustin (`dustin@socar.kr`), 카셰어링본부 > 카셰어링그룹 > 경기강원사업팀
- **Issue / 통합 협업**: 슬랙 DM 또는 본 리포의 GitHub Issues
- **데이터 정의 / 지표 질문**: 동일 채널 (BQ 사전 + 메모리에서 응답 가능)

---

_Last updated: 2026-05-12_
