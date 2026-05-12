# 구현 가이드 (Implementation Reference)

> README가 "무엇이 있는가"를 다룬다면, 이 문서는 "어떻게 구현되었는가"를 다룬다. 동료가 같은 로직을 자기 작업물에 클로닝할 수 있도록 함수 시그니처·계산 공식·SQL 패턴·JSON 스키마·LLM 프롬프트까지 모두 적었다.
>
> 모든 line 번호는 `update.py` / `server.py` 기준 (2026-05-12 스냅샷, 5,819 / 484 lines).

---

## 목차
1. [코드 구조 & 빌드 진입점](#1-코드-구조--빌드-진입점)
2. [TEAM_CONFIG 확장 / 권역 polygon](#2-team_config-확장--권역-polygon)
3. [BQ 쿼리 layer (`query_*`)](#3-bq-쿼리-layer-query_)
4. [분석 layer (`compute_*`)](#4-분석-layer-compute_)
5. [존 개설 시뮬레이션](#5-존-개설-시뮬레이션)
6. [LLM 평가 프롬프트 (풀텍스트)](#6-llm-평가-프롬프트-풀텍스트)
7. [충전 인프라 (매칭 + 혼잡도)](#7-충전-인프라-매칭--혼잡도)
8. [HTML/JS 렌더링 layer (`generate_index`)](#8-htmljs-렌더링-layer-generate_index)
9. [`server.py` 핸들러](#9-serverpy-핸들러)
10. [cache JSON 정확한 스키마](#10-cache-json-정확한-스키마)
11. [핵심 상수·매직넘버 사전](#11-핵심-상수매직넘버-사전)
12. [클로닝 시 자주 막히는 함정 8가지](#12-클로닝-시-자주-막히는-함정-8가지)

---

## 1. 코드 구조 & 빌드 진입점

```
update.py (5,819 lines)
├─ TEAM_CONFIG, _get_team_polygon, _expand_regions, _region1_sql  (1~170)
├─ run_bq, query_* × 16개                                          (180~1500)
├─ haversine_km, _point_in_polygon, _is_likely_gyeonggi          (2035~2135)
├─ compute_gaps, compute_reentry_zones, compute_growth_analysis  (2139~2735)
├─ reverse_geocode (Nominatim)                                    (2737)
├─ generate_index (HTML 직렬화, 약 2,000줄)                       (3150~5195)
├─ regenerate_from_cache                                           (5196)
├─ main, _build_html, update_demand, update_zone, generate_landing  (5351~5807)
└─ __main__ 인자 파싱 (--team / --demand / --zone / --regen)

server.py (484 lines)
├─ AUTH (비밀번호·쿠키)                                            (~50)
├─ Handler.do_GET / do_POST 라우팅                                 (110~190)
├─ handle_status, handle_ai_config                                  (195)
├─ handle_update_demand, handle_update_zone (subprocess + 쿨다운)   (208, 236)
├─ handle_simulate, handle_simulate_eval (update.py 함수 import)    (279, 314)
├─ handle_d2d_destinations                                          (298)
└─ handle_dashboard*, deploy_to_github                              (330~)
```

### 빌드 함수 4종

| 함수 | 진입 | BQ 호출 | HTML 재생성 | root index 복사 |
|---|---|:---:|:---:|:---:|
| `main(team_id)` | `update.py` 또는 `--team xxx` | ✅ 풀 | ✅ | ✅ |
| `update_demand(team_id)` | `--demand` | 접속·예약·부름만 | ✅ via `_build_html` | ✅ |
| `update_zone(team_id)` | `--zone` | 존·실적·EV만 | ✅ via `_build_html` | ✅ |
| `regenerate_from_cache(team_id)` | `--regen` | ❌ (cache만) | ✅ | ✅ |
| `generate_landing_page()` | `--team all` 종료 시 | ❌ | 루트만 (랜딩) | – |

3개 빌드 함수 끝부분에 동일 패턴이 들어있다:
```python
out_dir = _team_output_dir(team_id)            # ~/carsharing_demand_map/<team_id>/
path = os.path.join(out_dir, "index.html")
with open(path, "w") as f:
    f.write(html)
root_index = os.path.join(OUTPUT_DIR, "index.html")
if path != root_index:
    shutil.copy2(path, root_index)             # 단일팀 모드: 루트도 갱신
```

> **2026-05-12 학습**: 이 root copy 블록이 빠진 빌드 함수가 있으면 GitHub Pages 루트가 stale해진다. `_build_html`/`main`/`regenerate_from_cache` 세 곳에 모두 있어야 함.

---

## 2. TEAM_CONFIG 확장 / 권역 polygon

```python
TEAM_CONFIG = {
    'seoul':       {'name': '서울사업팀',       'regions': ['서울특별시', '인천광역시'],            'center': [37.5665, 126.978], 'zoom': 11, 'bbox': {…}},
    'gyeonggi':    {'name': '경기강원사업팀',   'regions': ['경기도', '강원도'],                     'center': [37.5, 127.8],     'zoom': 8,  'bbox': {…}},
    'chungcheong': {'name': '충청사업팀',       'regions': ['충청남도','충청북도','대전광역시','세종특별자치시'], …},
    'gyeongbuk':   {'name': '경북사업팀',       'regions': ['경상북도', '대구광역시'], …},
    'gyeongnam':   {'name': '부울경사업팀',     'regions': ['부산광역시', '울산광역시', '경상남도'], …},
    'honam':       {'name': '호남사업팀',       'regions': ['전라남도', '전라북도', '광주광역시'], …},
}
```

### `_get_team_polygon(team_id)`
1. `korea_provinces.json` 로드 (17개 시도 GeoJSON, code별 properties.name 한글)
2. `TEAM_CONFIG[team_id]['regions']`의 region명을 GeoJSON 명칭에 매핑 (`강원특별자치도 → 강원도`, `전북특별자치도 → 전라북도`, `세종특별자치시` 유지)
3. 해당 features를 `shapely.unary_union`으로 합쳐 prepared polygon 반환
4. 결과 캐싱 (`_TEAM_POLYGONS` 딕셔너리)

### `_region1_sql(team_id, alias='cz')`
BQ `region1 IN ('경기도', '강원도', '강원특별자치도')` 같은 IN 절을 만든다. `_REGION_ALIASES`로 강원특별자치도/전북특별자치도 별칭을 확장 (BQ 데이터는 시점별로 둘 다 존재할 수 있음).

```python
_REGION_ALIASES = {
    '강원도': ['강원도', '강원특별자치도'],
    '전북특별자치도': ['전북특별자치도', '전라북도'],
}
```

### `_is_likely_gyeonggi(lat, lng)` (line 2123)
경기도 polygon보다 빠른 휴리스틱 — `_in_team()` 게이트에서 polygon contains 호출 전 빠른 reject용.

### `haversine_km(lat1, lng1, lat2, lng2)` (line 2035)
표준 haversine 공식, R=6371.

---

## 3. BQ 쿼리 layer (`query_*`)

### 3.1 공통 패턴

모든 SQL은 다음 4가지 필터를 일관되게 적용한다:

| 필터 | 코드 패턴 | 의미 |
|---|---|---|
| 기간 | `WHERE timeMs >= TIMESTAMP('{THREE_MONTHS_AGO}', 'Asia/Seoul')` | 최근 90일 |
| 권역 | `{_region1_sql(team_id, 'cz')}` → `cz.region1 IN (…)` | 팀 관할 region1 |
| 존 활성 | `cz.state = 1 AND cz.imaginary IN (0,3,5) AND cz.visibility = 1` | 운영 중 존 + 분류 가상존 포함 |
| 예약 정상 | `r.state = 3 AND r.member_imaginary IN (0,9)` | 완료된 예약 + 실회원 |

`run_bq(sql)` (line 183): `bq query --use_legacy_sql=false --format=json` subprocess 호출. `max_rows`는 함수마다 다름. 결과는 JSON 파싱.

### 3.2 함수 카탈로그 (16개)

| 함수 | 출력 | 핵심 테이블 | line |
|---|---|---|---|
| `query_access` | `[{lat, lng, access_count}]` | `socar_server_3.GET_MARKERS{,_V2}` ROUND(lat,3)·ROUND(lng,3) GROUP BY | 192 |
| `query_reservation` | `[{lat, lng, reservation_count}]` | `soda_store.reservation_v2` ROUND lat/lng | 227 |
| `query_reservation_by_zone_region` | region별 예약 (zone 소속 차량 기준) | `reservation_v2` + `car_info` + `carzone_info` | 255 |
| `query_dtod` | `[{lat, lng, call_count}]` HAVING ≥ 12 | `tianjin_replica.reservation_dtod_info` start_lat·start_lng | 274 |
| `query_d2d_destinations(zone_id)` | 도착지 좌표 분포 | 부름 끝 좌표(end_lat/end_lng) | 308 |
| `query_zones` | 운영 존 메타 + 차량 수 | `carzone_info` + `car_info` | 920 |
| `query_socar_supply_by_region` | region별 운영 차량/존 수 | `profit_socar_car_daily` | 940 |
| `query_reservation_timeline` | 존×요일×시간 혼잡 매트릭스 | `reservation_info` + `car_info` + `carzone_info` | 969 |
| `query_parking_contract` | 주차 계약·정산·요율 | `socar_zone.{parking,parking_contract,settlement_type_policy,rent_policy,parking_provider}` 6중 LEFT JOIN | 1019 |
| `query_gcar_zones` | 그린카 운영존 | `greencar.gcar_zone_info_log` MAX(date) 스냅샷 | 1082 |
| `query_ev_chargers` | 환경부 충전소 statId별 집계 | 환경부 API (BQ 아님) | 1145 |
| `query_ev_car_zones` | 존별 EV 차량 수 + 충전보장 수 | `car_info` LIKE `'%전기차%'` / `'%충전보장%'` | 1315 |
| `query_ev_virtual_zones` | 충전보장 가상존 → original_zone 매핑 | `carzone_info` imaginary 분류 | 1347 |
| `query_closed_zones` | 폐쇄 존 실적 누계 | `profit_socar_car_daily` opr_day=0 HAVING | 1420 |
| `query_zone_profit` | 존별 28일 실적 (revenue/GP/가동률/건당) | `profit_socar_car_daily` + `operation_per_car_daily_v2` | 1494 |
| `query_dashboard_metrics` | 팀 주간 KPI | weekly aggregation | 1546 |
| `query_dashboard_by_region(region_level)` | region2/3별 주간 KPI | weekly × region group | 1780 |
| `query_advance_utilization` | 사전가동률 + demand_grade | `socar_biz_base.operation_per_car_daily_v2_snapshot` | 2285 |
| `query_weekly_trends` | 주간 접속/예약/공급 추이 | 다중 weekly UNION | 2429 |

### 3.3 가동률 계산 (모든 함수에서 동일)

```sql
SAFE_DIVIDE(SUM(o.op_min), SUM(o.dp_min) - SUM(o.bl_min)) * 100
```
- `op_min`: 운영중 분 (operation)
- `dp_min`: 배치 분 (display) — 분모
- `bl_min`: 블락 분 (blocked) — 분모에서 제외

### 3.4 대당매출 (28일 기준, 가중평균)

```sql
ROUND(SAFE_DIVIDE(SUM(p.revenue) * 28, NULLIF(SUM(p.opr_day), 0))) AS revenue_per_car_28d
```
SUM(revenue) × 28 / SUM(opr_day). opr_day(운영일수)가 분모인 점에 유의 — 단순 car 수가 아니다.

### 3.5 건당매출

```sql
ROUND(SUM(p.revenue) / NULLIF(SUM(p.nuse), 0), 0) AS revenue_per_res
```
`nuse` = 이용건수.

---

## 4. 분석 layer (`compute_*`)

### 4.1 `compute_gaps(access_data, reservation_data, zones_data, team_id)` — 미진출 지역 (line 2139)

```
입력:  access [{lat,lng,access_count}]  (ROUND(lat,3) 격자)
       reservation, zones_data
출력:  최대 50개 gap 후보 (access_count 내림차순)
```

**알고리즘**:
1. 팀 polygon `_in_team(lat,lng)` 필터로 인접 시도 좌표 제거
2. 접속 격자 만들기 (`access_grid[key] = sum(count)`)
3. 각 격자에 대해:
   - `access_count >= MIN_ACCESS(90)` — 3개월 90건 미만 제외
   - 모든 존과의 최단거리 `min_dist`:
     - `min_dist > MIN_DIST_KM` (서울 0.3 / 경기 0.5 / 그 외 0.8)
     - `min_dist < max_gap_dist` (서울 3.0 / 그 외 10.0)
4. 통과한 격자 중심에서 **반경 500m bbox**의 접속/예약 합산 (시뮬레이션과 동일 기준)
5. `access_count`는 월평균으로 환산 (`/ 3`)
6. 내림차순 정렬 후 top 50

### 4.2 `compute_reentry_zones(closed_data, access_data, reservation_data, zones_data, team_id)` (line 2203)

폐쇄존 중 재진입 추천. **모든 조건을 AND**.

| 조건 | 룰 |
|---|---|
| 0 | zone_name에 부적합 키워드 미포함: `공영주차장 / 꼬끼오 / 마트 / 매각 / 비즈니스 / 시승 / 장착 / 캐리어 / 캠핑카 / 경매 / 플랜` |
| 1 | `hist_car_count >= 1` (평균 운영대수) |
| 2 | `operation_days >= 30` |
| 2.5 | `revenue_per_car_28d >= 1,600,000원` (대당매출 160만원, 4주 환산) |
| 3 | 반경 **300m** 내 현재 운영 존 없음 |
| 4 | 반경 **500m** 내 접속/예약 0이 아님 |
| 5 | 후속 필터: `nearby_access >= 100/월` |

정렬: `-(nearby_access + nearby_res)`, top 50.

### 4.3 `compute_growth_analysis(weekly_data, zones_data, team_name)` (line 2542)

권역 region2별 **점유율 시계열** 산출 → 성장/감소 분류.

**계산**:
1. region2별 주간 `{access, res, cars}` 매트릭스 구축
2. 팀 전체 주간 합 `gg_total[week]` 계산 (시즈널리티 베이스라인)
3. 각 region2의 주차별 점유율 = `region_value / gg_total[week] * 100`
4. 양 끝 0값 트림 (불완전 주차)
5. `_half_change(share)` — 전반기 평균 vs 후반기 평균 (% 변화율)
6. 분류:

   | `access_growth` | `car_growth` | status |
   |---|---|---|
   | > 0 (수요 성장) | < -1 (감차됨) | **점검 필요** |
   | > 0 | > 1 (증차됨) | 대응 진행 중 |
   | > 0 | -1 ~ 1 | **증차 검토** |
   | ≤ 0 (수요 감소) | > 1 (증차됨) | **점검 필요** |
   | ≤ 0 | < -1 (감차됨) | 대응 진행 중 |
   | ≤ 0 | -1 ~ 1 | **감차 검토** |

7. 출력: `{growth: [...], decline: [...]}`, 각각 `res_growth` 정렬

`_half_change(vals)`:
```python
def _half_change(vals):
    if len(vals) < 4: return 0
    mid = len(vals) // 2
    first = sum(vals[:mid]) / mid
    second = sum(vals[mid:]) / (len(vals) - mid)
    if first == 0: return 0
    return round((second - first) / first * 100, 1)
```

### 4.4 `compute_ev_congestion()` (line 1272)

```
입력:  .cache/ev_status_log/*.jsonl  (collect_ev_status.py가 30분마다 append)
출력:  {statId: [h0%, h1%, …, h23%]}  (값 -1은 데이터 없음)
```

각 jsonl 줄 형식:
```json
{"ts":"202605120030","hour":0,"weekday":1,"stations":{"ME174131":{"charging":1,"total":2}, …}}
```

집계:
- `station_hourly[sid][hour]['charging']` += charging
- `station_hourly[sid][hour]['total']` += total
- 24시간 배열 생성: `rate = round(charging/total*100)` (total>0인 경우만, 아니면 -1)

### 4.5 `match_ev_to_zones(ev_data, zones_data, radius_km=0.1)` (line 1227)

각 운영존에 100m 이내 충전소를 매칭하고, "동일 현장" 판별까지 한다.

**동일 현장 판별 룰** (`is_same`):
1. 존명/주차장명에서 공백 제거
2. 충전소명에 포함되거나 그 반대 → `is_same = True`
3. 또는 3글자 이상 부분 매칭

존에 추가되는 필드:
- `ev_stations` (개수), `ev_fast`, `ev_slow`, `ev_total`
- `ev_same_total`, `ev_same_fast`, `ev_same_slow` (동일 현장만)
- `ev_detail: [{name, fast, slow, total, dist(m), same, congestion}]`

**충전보장 EV 존 fallback**: `has_ev_zone=True`이고 동일 현장 매칭이 없으면 가장 가까운 충전소를 `same=True`로 강제.

---

## 5. 존 개설 시뮬레이션

### 5.1 `simulate_zone(lat, lng, radius_km=0.5, team_id)` (line 408)

**입력**: 임의 좌표 + 반경 + 팀.
**소요**: BQ 쿼리 4개 + 인근 벤치마크 계산 + 카니발 보정 → 약 5~15초.

#### 단계별 계산

**Step 1. 반경 → 위경도 bbox**
```python
dlat = radius_km / 111.0
dlng = radius_km / (111.0 * abs(math.cos(math.radians(lat))))
lat_min, lat_max = lat - dlat, lat + dlat
lng_min, lng_max = lng - dlng, lng + dlng
```

**Step 2. 4개 BQ 쿼리 (각각 단일 row 반환)**
- `access_sql`: `GET_MARKERS_V2 UNION ALL GET_MARKERS`, bbox + 3개월 윈도우, `COUNT(*)`
- `res_sql`: `reservation_v2` bbox + `state=3 AND member_imaginary IN (0,9)` + 3개월
- `dtod_sql`: `reservation_dtod_info` bbox + 3개월
- `conv_sql`: 해당 region2의 모든 region3 월별 res_cnt, car_cnt, avg_hours_per_res

**Step 3. 90→7일 환산**
```python
num_weeks = 90 / 7
weekly_access = round(total_access_90d / num_weeks)
weekly_res = round(total_res_90d / num_weeks)
weekly_dtod = round(total_dtod_90d / num_weeks)
```

**Step 4. region 결정 (가장 가까운 기존 존 기준)**
```python
nearest_zone = min(zones, key=lambda z: dist(z, (lat,lng)))
region2 = nearest_zone.region2
region3 = nearest_zone.region3
```

**Step 5. 전환율 계산** (월별 minimum, fallback 체인)
1. cached `access.json`을 모든 좌표에 대해 "가장 가까운 존의 region"로 배정 → `region_access[r2]['by_r3'][r3]`
2. `conv_rows`(월별 res by region3)와 결합:
   - 월별 `res_cnt / monthly_r3_access` 리스트 → `r3_monthly_convs`
   - `conv_rate = min(r3_monthly_convs)` ← 보수적 선택
3. region3 데이터 부족 → region2 월별 min
4. region2도 없음 → 팀 전체 평균

**Step 6. 예상 주간 예약**
```python
base_res = round(weekly_access * conv_rate)
dtod_conversion = 0.5                              # 부름 → 일반 예약 전환율 상수
dtod_additional = round(weekly_dtod * dtod_conversion)
est_weekly_res = base_res + dtod_additional
```

**Step 7. 인근 벤치마크 (반경 3km, 거리 가중평균)**
```python
bench_radius = 3.0
for z in zones:
    zd = haversine_km(...)                          # km
    if zd <= 3.0 and z.car_count > 0:
        w = 1 / max(zd, 0.1)                        # 가까울수록 가중 ↑
        w_rev  += revenue_per_car_28d * w
        w_gp   += gp_per_car_28d * w
        w_util += utilization_rate * w
        w_rpr  += revenue_per_res * w
        w_total += w

est_rev_per_car = round(w_rev / w_total)
est_gp_per_car  = round(w_gp  / w_total)
avg_util         = w_util / w_total
revenue_per_res  = round(w_rpr / w_total)
```

**Step 8. 추천 공급대수** (역산)
```python
TARGET_REV_PER_CAR = 1_600_000                     # 4주 대당 매출 목표
est_monthly_revenue = est_weekly_res * 4 * revenue_per_res
cars_needed = est_monthly_revenue / TARGET_REV_PER_CAR
raw_recommended_cars = max(0, math.floor(cars_needed))
```

**Step 9. 카니발리제이션 보정** (반경 0.5km)
```python
cannibal_cars = 0.0
for z in zones:
    zd = haversine_km(...)
    if zd <= radius_km and z.car_count > 0:
        overlap = 1 - (zd / radius_km)              # 0~1, 가까울수록 ↑
        cannibal_cars += z.car_count * overlap
recommended_cars = max(0, math.floor(raw_recommended_cars - cannibal_cars))
is_recommend = recommended_cars >= 1
```

**Step 10. 수요 등급 (백분위)**
모든 `access.json`의 access_count를 정렬하여 현재 좌표의 90일 접속이 차지하는 백분위:
- ≥ 95% → `S`
- ≥ 80% → `A`
- ≥ 50% → `B`
- < 50% → `F`

**Step 11. 과거 존 이력 (반경 300m)**
별도 SQL (`hist_sql`) — `zone.type IN ('ZONTP_NORMAL','ZONTP_D2D_STATION','ZONTP_D2D_PRIORITY','ZONTP_D2D_CALL')` + 어제 기준 `opr_day=0` (이미 폐쇄) zone들. 한 zone당 first_date / last_date / operation_days / 대당매출 / 대당GP / 가동률.

과거 존 실적이 있으면 `est_rev_per_car`을 **7:3 블렌딩**:
```python
est_rev_per_car = round(est_rev_per_car * 0.7 + hist_rev_avg * 0.3)
```

**Step 12. 주차비 추천 범위**
- 벤치마크 주차비 (거리 가중평균)
- 과거 존 주차비 × `(1.03)^years` (CPI 3% 보정)
- 둘 다 있으면 min~max, 하나만 있으면 ±15%, 둘 다 없으면 0
- `parking_mid = (low + high) / 2`

**Step 13. GPM 시뮬레이션 (5 시나리오)**
```python
for pct in [-0.25, -0.10, 0, 0.10, 0.25]:
    pc = round_1000(parking_mid * (1 + pct))
    parking_diff = pc - bench_avg_parking
    gp = est_gp_per_car - parking_diff               # 주차비 차이만큼 GP 보정
    gpm = round(gp / est_rev_per_car * 100, 1)
```

#### 출력 스키마 (`simulate_zone` 반환)

```typescript
{
  // 입력 반사
  lat, lng, radius_km

  // 위치
  region2, region3
  nearest_zone, nearest_zone_dist_km
  demand_grade,                      // S/A/B/F
  demand_percentile

  // 수요
  weekly_access, weekly_res, weekly_dtod
  total_access_90d, total_res_90d

  // 전환율
  conv_rate, conv_level              // "강남구 (월별 min)" 같은 라벨
  conv_rank, conv_total_r3           // region2 내 region3 순위
  r2_avg_conv, r2_max_conv, r2_min_conv
  avg_hours_per_res, hours_level

  // 예약 예측
  base_res, dtod_additional, est_weekly_res

  // 벤치마크
  bench_zone_count
  est_rev_per_car, est_gp_per_car, avg_util, revenue_per_res
  est_monthly_revenue

  // 공급
  raw_recommended_cars
  cannibal_cars, nearby_total_cars, nearby_zone_count
  recommended_cars, is_recommend

  // 과거 존
  hist_zones: [{zone_id, zone_name, zone_type, is_d2d_call,
                first_date, last_date, operation_days,
                total_cars_ever, revenue_per_car_28d, gp_per_car_28d,
                avg_utilization, monthly_avg_res, revenue_per_res}]

  // 주차비
  bench_avg_parking, bench_parking_count
  hist_avg_parking, hist_parking_costs: [{zone_name, original, adjusted, years}]
  parking_range_low, parking_range_high, parking_mid

  // GPM
  gpm_scenarios: [{parking_cost, gp_per_car, gpm, is_mid}]
}
```

---

## 6. LLM 평가 프롬프트 (풀텍스트)

`evaluate_simulation_llm(sim_data, team_id)` (line 338): 위 출력을 GPT-5.2 / Claude 3.5 Sonnet 병렬 호출. 클라이언트는 사내 LLM gateway (`SOCAR_LLM_BASE`, `SOCAR_LLM_KEY`).

**프롬프트 전문**:
```
당신은 쏘카(SOCAR) 카셰어링 존 개설 전략 분석가입니다.
아래 시뮬레이션 데이터를 분석하세요.

[위치] {region2} {region3}
[수요 등급] {grade} ({team_name} 접속 격자 상위 {100-percentile:.0f}%)
[반경 500m 주간 수요] 접속 {weekly_access:,} / 예약 {weekly_res:,} / 부름 {weekly_dtod:,}
[전환율] {conv_rate*100:.2f}% ({conv_level}) — {region2} 내 {conv_total_r3}개 지역 중 {conv_rank}위
         (평균 {r2_avg:.2f}%, 최고 {r2_max:.2f}%, 최저 {r2_min:.2f}%)
[예상 주간 예약] {est_weekly_res}건 (접속기반 {base_res} + 부름전환 {dtod_additional})
[인근 벤치마크 ({bench_zone_count}개 존)] 대당매출 {est_rev_per_car:,}원/4주,
         대당GP {est_gp_per_car:,}원/4주, 가동률 {avg_util:.1f}%
[매출 추정] 건당매출 {revenue_per_res:,}원, 추정 월매출 {est_monthly_revenue:,}원
[대당 목표매출] 1,600,000원/4주
[수요 기반 적정대수] {raw_recommended_cars}대
[카니발리제이션] 반경 500m 내 기존 {nearby_zone_count}개 존 {nearby_total_cars}대,
         카니발 차감 -{cannibal_cars}대
[추천 공급대수] {recommended_cars}대
[최근접 존] {nearest_zone} ({nearest_zone_dist_km}km)
[과거 존 이력] {N}건
  - {zone_name}: {first_date}~{last_date} ({operation_days}일 운영),
    대당매출 {revenue_per_car_28d:,}원, GP {gp_per_car_28d:,}원, 가동률 {avg_utilization}%

접속량과 지역 전환율을 바탕으로 계산된 주 예약 추정 수치를 활용하여 해당 지역의
수요 포텐셜을 평가하고, 대당 매출 160만원을 목표로 건당 매출 × 주 예약 건으로
계산되는 수요 기반 적정 공급 대수, 카니발리제이션이 보정된 추천 공급대수 수치를
신뢰할 수 있는지 판단하세요. 신뢰할 수 없다면 어떤 이유에서이며 그 근거를 상세하게
기술하세요. 마지막으로 인근(반경 500m) 존의 실적과 해당 지역에서 과거 운영했던
존의 실적 등 그 외 지표를 적극 활용하여 최종 개설 여부 판단을 내려주세요.

다음 형식으로 작성하세요. 첫 줄은 반드시 추천 여부로 시작하고, 각 항목 사이에
빈 줄을 넣으세요:
개설 추천 O 또는 개설 비추천 X

[1] 수요 식별: (1~2문장)

[2] 추천 공급 대수 검증: (1~2문장)

[3] 최종 코멘트: (1~2문장)
```

**호출 파라미터**:
- GPT: `dev/gpt-5.2-chat`, max_tokens=8000, temperature=0.3
- Claude: `prod/claude-3.5-sonnet`, max_tokens=800, temperature=0.3
- `concurrent.futures.ThreadPoolExecutor(max_workers=2)` 병렬, timeout=120s

**반환**: `{'gpt': str, 'claude': str}`

---

## 7. 충전 인프라 (매칭 + 혼잡도)

### 7.1 `query_ev_chargers(team_id)` (line 1145)

환경부 API 호출 흐름:
1. `EV_CHARGER_ZCODES[team_id]`의 각 zcode에 대해 페이지네이션 (`numOfRows=9999`)
   - 서울: `['11']`
   - 경기강원: `['41', '51']` (강원=특별자치도 코드)
   - 충청: `['43', '44', '30', '36']`
   - 경북: `['47', '27']`
   - 경남: `['48', '26', '31']`
   - 호남: `['45', '46', '29']`
2. `chgerType` 변환 → 급속 여부 (1,3,4,5,6 = 급속)
3. `statId` 단위로 그룹: 충전소 = `{statId, statNm, addr, lat, lng, busiNm, useTime, fast, slow}`
4. 로밍 키워드 매칭:
   ```python
   EVERON_KEYWORDS = ['한국전기차충전', '한국전자금융', 'NICE인프라', '나이스차저',
                      '현대엔지니어링', …]
   CHARGEV_KEYWORDS = ['한국전기차충전', '현대엔지니어링', '제주전기자동차',
                       '휴맥스', 'HUMAX', …]
   ```
5. `team_poly.contains(Point(lng, lat))` 필터 (한 zcode가 인접 시도 좌표를 일부 반환할 수 있음)

API 키: `EV_CHARGER_API_KEY` 코드 하드코딩 (line 1134).
API URL: `http://apis.data.go.kr/B552584/EvCharger/getChargerInfo?serviceKey=…&numOfRows=9999&pageNo={page}&dataType=JSON&zcode={zcode}`

### 7.2 `collect_ev_status.py` (충전기 상태 cron)

30분 간격 cron. `getChargerInfo`로 전체 충전기의 `stat` 코드를 받아 `.cache/ev_status_log/YYYY-MM-DD.jsonl`에 한 줄 추가:
```json
{"ts":"202605120030","hour":0,"weekday":1,
 "stations":{"ME174131":{"charging":1,"total":2}, …}}
```

stat 코드:
- `1` 통신이상 / `2` 충전대기 / `3` 충전중 / `4` 운영중지 / `5` 점검중 / `9` 상태미확인
- `charging` 카운트 = stat `3`인 chgerId 수
- `total` 카운트 = 전체 chgerId 수

### 7.3 `compute_ev_congestion` → popup chart

JSON에 들어가는 `congestion`는 길이 24 정수 배열 (0~100, 데이터 없음은 -1). HTML 안의 `evCongestionChart(congestion)`이 이 배열을 받아 24개 막대를 그린다.

---

## 8. HTML/JS 렌더링 layer (`generate_index`)

### 8.1 데이터 직렬화

`generate_index` (line 3150)는 약 2,000줄짜리 함수로, 모든 데이터를 `var xxxData = …;` 형태로 JS 변수에 embed하고 HTML을 한 번에 만든다.

JS 측 핵심 데이터 변수:
```javascript
var teamConfig    = {name, center, zoom, ...};
var accessData    = [[lat, lng, count], ...];     // [tuple, tuple]
var resData       = [[lat, lng, count], ...];
var dtodData      = [[lat, lng, count], ...];
var zonesData     = [{zone_id, zone_name, lat, lng, car_count, region1/2/3, …, ev_zone, ev_detail, has_ev, has_charged_ev}, ...];
var gapsData      = [{lat, lng, access_count, ...}, ...];
var reentryData   = [{zone_id, lat, lng, hist_car_count, operation_days, ...}, ...];
var gcarData      = [{zone_id, zone_name, lat, lng, total_cars, region2, sig_name}, ...];
var closedData    = [{zone_id, lat, lng, ...}, ...];
var evData        = [{statId, statNm, lat, lng, fast, slow, total, busiNm, everon, chargev, congestion[24]}, ...];
var marketShareData;
var timelineData  = {zone_id: [{...}, ...]};  // 예약 타임라인
var analysisData  = {growth: [...], decline: [...]};
var ngrokUrl      = "{ngrok_url}";  // ngrok 도메인 (HTML 빌드 시 .ngrok_url에서 읽음)
```

### 8.2 주요 JS 함수

| 함수 | 위치 (대략) | 역할 |
|---|---|---|
| `makeZoneIcon(z)` | 3880 | 운영존 마커 SVG icon (car_count 따라 크기·색) |
| `makePopup(z)` | popup 빌드 | 차량/실적/계약/EV/타임라인 풀 카드 |
| `evCongestionChart(congestion)` | 2998 | 충전소 popup 안 0~23시 막대그래프 SVG |
| `buildChargerHtml(evDetail)` | 3076 | 운영존 popup 안 충전기 섹션 |
| `rebuildEvLayer()` | 4051 | 충전 인프라 필터 (전체/에버온/차지비) 다시 그리기 |
| `renderReentryList(filter)` | 3990 | 재진입 패널 리스트 + 클릭 이동 |
| `d2dToggleZones()` | 5170 | D2D 도착지 모드 토글 |
| `runUpdateDemand()` / `runUpdateZone()` | 사이드바 버튼 → `/api/update-demand` 호출 |

### 8.3 Leaflet 레이어 group 14개

```javascript
var accessLayer   = L.layerGroup();    // 앱 접속 (circleMarker)
var resLayer      = L.layerGroup();    // 예약 생성
var dtodLayer     = L.layerGroup();    // 부름 호출
var zoneLayer     = L.layerGroup();    // 운영존 (marker + icon)
var gapLayer      = L.layerGroup();    // Gap
var reentryLayer  = L.layerGroup();    // 재진입 (circleMarker pulse)
var evLayer       = L.layerGroup();    // 충전 인프라
var gcarLayer     = L.layerGroup();    // 그린카
var closedLayer   = L.layerGroup();    // 폐쇄존
var d2dDestLayer  = L.layerGroup({pane:'d2dPane'});  // 부름 도착지
var measureLayer  = L.layerGroup();    // 거리/반경 측정
// EV 운영존 / 충전보장형은 zoneLayer 안에서 필터링
// Market Share / 미진출 / 재진입 패널은 별도 DOM
```

### 8.4 사이드바 버튼 ID

```
toggleAccess / toggleRes / toggleDtod          (수요)
toggleZones                                     (운영존)
toggleGcar / toggleClosed                       (경쟁/폐쇄)
toggleEvZones / toggleChargedOnly / toggleEvInfra  (EV)
toggleGap / toggleReentry / toggleMarketShare   (분석)
updateDemandBtn / updateZoneBtn                 (백그라운드 업데이트)
simCtxBtn                                       (시뮬레이션)
searchModeAddr / searchModeZone                 (검색)
```

### 8.5 API base URL 동적 결정 (line 4782, 5071)

```javascript
var isLocal = (location.hostname === 'localhost'
            || location.hostname === '127.0.0.1'
            || location.hostname.endsWith('.ngrok-free.dev'));
var simApiBase = isLocal ? '' : '{ngrok_url}';  // 빌드 시점에 .ngrok_url 임베드

fetch(simApiBase + '/api/simulate', {
    method: 'POST',
    headers: {
        'Content-Type': 'application/json',
        'ngrok-skip-browser-warning': 'true',
    },
    body: JSON.stringify({lat, lng, radius: 0.5, team_id: 'gyeonggi'})
});
```

---

## 9. `server.py` 핸들러

### 9.1 인증

```python
AUTH_PASSWORD = 'socar3316!'
AUTH_TOKEN    = hashlib.sha256(AUTH_PASSWORD.encode()).hexdigest()[:32]
AUTH_COOKIE_NAME = 'socar_auth'
```
- `POST /auth/login` → 폼 파라미터 `password` 검증 → `Set-Cookie: socar_auth=…; Max-Age=86400; SameSite=Lax`
- `/api/*` 는 인증 우회 (브라우저가 자동으로 쿠키 같이 보냄)
- localhost는 `_needs_auth()` 가 False 반환 (host 헤더 검사)

### 9.2 엔드포인트별 핸들러

| Endpoint | Method | Body / Query | 동작 |
|---|---|---|---|
| `/auth/login` | POST | `password=...` | 쿠키 발급 → 302 `/` |
| `/api/status` | GET | – | `{last_demand, last_zone}` |
| `/api/ai-config` | GET | – | `{configured, model}` |
| `/api/update-demand` | POST | `{team_id}` | subprocess `update.py --demand --team xxx`, 종료 후 `deploy_to_github()` |
| `/api/update-zone` | POST | `{team_id}` | 동일 + **5분 쿨다운** (`.last_update_zone` 비교) |
| `/api/update` | POST | `{team_id}` | `update-demand` 별칭 |
| `/api/simulate` | POST | `{lat, lng, radius, team_id}` | `simulate_zone()` 실시간 호출 |
| `/api/simulate-eval` | POST | sim_data | `evaluate_simulation_llm()` |
| `/api/d2d-destinations` | POST | `{zone_id}` | `query_d2d_destinations()` |
| `/api/dashboard` | GET | `?team_id=xxx` | dashboard JSON 조합 |
| `/api/dashboard-analyze` | POST | – | 대시보드 보조 분석 |
| `/api/dashboard-region3` | POST | – | region3 단위 KPI |

### 9.3 `deploy_to_github()`

`/api/update-*` 성공 시 자동 호출. `git add . && git commit && git push origin main` subprocess.

### 9.4 듀얼 ngrok Host 분기

```python
def _send_login_page(self, error=False):
    html = LOGIN_HTML
    host = self.headers.get('Host', '')
    if 'dustin.ngrok.app' in host:
        html = html.replace('경기강원 수요/인프라 지도', '<img src="data:image/png;base64,…">')
    …
```
같은 서버지만 host에 따라 다른 로고/타이틀.

---

## 10. cache JSON 정확한 스키마

`gyeonggi` 권역 기준 2026-05-12 스냅샷. 키 이름·타입은 모든 권역에서 동일.

### `zones.json` — `list[obj]`, 1,022건
```typescript
{
  zone_id: int,           // BQ `carzone_info.id`
  zone_name: string,
  parking_name: string,
  lat: float, lng: float,
  region1: string,        // "경기도" / "강원특별자치도"
  region2: string,        // "성남시" / "춘천시"
  region3: string,        // "분당구" / "효자동"
  address: string,
  car_count: int,         // 운영 중 차량 수
  imaginary: int,         // 0=일반, 3·5=가상존, 1=충전보장 가상존 등
  is_d2d_car_exportable: bool,  // 부름 가능 여부
  // _build_html에서 추가되는 필드
  ev_zone?: {zone_id, zone_name, car_count, imaginary, …},  // 충전보장 가상존
  ev_car_count: int, ev_charged_count: int,
  has_ev: bool, has_charged_ev: bool,
  ev_stations: int, ev_fast: int, ev_slow: int, ev_total: int,
  ev_same_total: int, ev_same_fast: int, ev_same_slow: int,
  ev_detail: [{name, fast, slow, total, dist(m), same, congestion[24]?}],
}
```

### `access.json` — `list[obj]`, 10,000건
```typescript
{ lat: float, lng: float, access_count: int }  // ROUND(lat,3) 격자
```

### `reservation.json` — `list[obj]`, 10,000건
```typescript
{ lat: float, lng: float, reservation_count: int }
```

### `dtod.json` — `list[obj]`, 2,025건 (HAVING ≥ 12)
```typescript
{ lat: float, lng: float, call_count: int }
```

### `ev_chargers.json` — `list[obj]`, 29,255건
```typescript
{
  statId: string,         // 환경부 충전소 ID
  statNm: string, addr: string,
  lat: float, lng: float,
  busiNm: string,         // 사업자명
  useTime: string,        // "24시간 이용가능"
  fast: int, slow: int, total: int,
  everon: bool, chargev: bool,
  congestion?: [int]      // length 24, value 0~100 or -1
}
```

### `profit.json` — `dict[zone_id_str → obj]`, 1,135건
```typescript
{
  total_revenue: int,
  revenue_per_car_28d: int,
  gp_per_car_28d: int,
  utilization_rate: float,    // 0~100
  revenue_per_res: int,
}
```

### `gaps.json` — `list[obj]`, 약 26건
```typescript
{
  lat, lng,
  access_count: int,         // 월평균
  reservation_count: int,    // 월평균
  nearest_zone_km: float,
  name: string,              // 역지오코딩 한글 주소
  region1: string,
}
```

### `closed.json` — `list[obj]`, 1,140건
```typescript
{
  zone_id, zone_name, parking_name, lat, lng,
  region2, region3, address,
  imaginary, state,             // state=0 폐쇄
  hist_car_count: float,         // 평균 운영 대수
  operation_days: int,
  first_date: string, last_date: string,
  revenue_per_car_28d: float,
  gp_per_car_28d: float,
  utilization_rate: float,
}
```

### `parking_contract.json` — `dict[zone_id_str → obj]`, 5,000건
```typescript
{ provider_name: string, settlement_type: string, price_per_car: int }
```

### `gcar.json` — `list[obj]`, 402건
```typescript
{ zone_id, zone_name, lat, lng, region2, sig_name, total_cars: int }
```

### `ev_virtual_zones.json` — `dict[original_zone_id_str → obj]`, 47건
```typescript
{
  zone_id: int,              // 가상존 ID
  zone_name: string,
  car_count: int,
  imaginary: int,
  is_d2d: bool,
  total_revenue: int,
  revenue_per_car_28d: int,
  gp_per_car_28d: int,
  utilization_rate: float,
}
```

### `ev_car_zones.json` — `dict[zone_id_str → obj]`, 56건
```typescript
{ ev_car_count: int, ev_charged_count: int }
```

### `timeline.json` — `dict[zone_id_str → list]`, 1,049건
각 zone당 예약 리스트 (요일×시간 혼잡 시각화용).

### `advance_util.json` — `dict`, 4 keys
```typescript
{
  now: { w0: [...], w1: [...], w2: [...] },   // 이번 주 / +1 / +2 주차
  yoy: { w0: [...], w1: [...], w2: [...] }    // 전년 동기
}
```

### `supply_demand.json` — `dict`
```typescript
{ growth: [{region2, region1, access_weekly, res_weekly, cars, …, status, …trend}], decline: [...] }
```

### `weekly_trends.json` — `dict`
```typescript
{ access: [...], res: [...], supply: [...] }
```

### `dashboard_metrics.json` / `dashboard_region2.json` / `dashboard_region3.json` — `dict`
주간 단위 KPI. 키: `weekly`, `daily`, `summary` 등 (정확한 키는 `query_dashboard_metrics`/`query_dashboard_by_region` 출력 참조).

---

## 11. 핵심 상수·매직넘버 사전

| 상수 | 값 | 위치 | 의미 |
|---|---|---|---|
| `THREE_MONTHS_AGO` | 오늘 - 90일 | global | BQ 윈도우 시작 |
| `TODAY` | `now.strftime('%Y-%m-%d')` | global | – |
| 데이터 기간 | 90일 (`num_weeks = 90/7`) | simulate_zone L418 | 주간 환산 분모 |
| 시뮬레이션 반경 | 0.5km (`radius_km=0.5`) | simulate_zone L408 | 수요 집계 + 카니발 |
| 시뮬레이션 벤치마크 반경 | 3.0km (`bench_radius`) | simulate_zone L693 | 인근 존 실적 가중평균 |
| 시뮬레이션 hist 반경 | 0.3km (`hist_dlat`) | simulate_zone L508 | 과거 존 이력 SQL |
| 시뮬레이션 hist 블렌딩 | 7:3 | simulate_zone L802 | 현재 추정 70% + 과거 30% |
| TARGET_REV_PER_CAR | 1,600,000원 | simulate_zone L728 | 대당 매출 4주 목표 |
| dtod_conversion | 0.5 | simulate_zone L680 | 부름 → 일반 예약 전환율 |
| 수요 등급 임계 | S≥95, A≥80, B≥50, F<50 | simulate_zone L762 | 백분위 등급 |
| INFLATION_RATE | 0.03 | simulate_zone L809 | 과거 주차비 CPI 보정 |
| GPM 시나리오 | [-0.25, -0.10, 0, 0.10, 0.25] | simulate_zone L864 | 주차비 5단계 |
| Gap MIN_ACCESS | 90 (3개월 누적) | compute_gaps L2171 | 격자 컷 |
| Gap MIN_DIST_KM | 서울 0.3 / 경기 0.5 / 그 외 0.8 | compute_gaps L2172 | 존 부재 임계 |
| Gap RADIUS_KM | 0.5 | compute_gaps L2173 | 격자 주변 수요 집계 |
| Gap max_gap_dist | 서울 3.0 / 그 외 10.0 | compute_gaps L2187 | 너무 먼 격자 제외 |
| Gap top N | 50 | compute_gaps L2200 | – |
| 재진입 hist_car_count | ≥ 1 | compute_reentry L2236 | 평균 운영대수 |
| 재진입 operation_days | ≥ 30 | L2239 | – |
| 재진입 revenue_per_car_28d | ≥ 1,600,000 | L2242 | – |
| 재진입 차단 반경 | 0.3km (현재 존) | L2246 | – |
| 재진입 수요 반경 | 0.5km | L2224 | – |
| 재진입 nearby_access | ≥ 100/월 | L2280 | – |
| 재진입 top N | 50 | L2282 | – |
| 재진입 제외 키워드 | 공영주차장, 꼬끼오, 마트, 매각, 비즈니스, 시승, 장착, 캐리어, 캠핑카, 경매, 플랜 | L2228 | – |
| EV 매칭 반경 | 0.1km (100m) | match_ev_to_zones | 운영존-충전소 동일현장 |
| EV 동일현장 임계 | 부분 문자열 ≥ 2 또는 3-gram | L1244 | – |
| EV 인프라 표시 임계 | total ≥ 3 AND (everon OR chargev) | update.py L4030 | 사이드바 표시 |
| 부름 표시 임계 | call_count ≥ 12 (HAVING) | query_dtod | – |
| 격자 limit 공식 | `max(500, min(3000, zones*3))` 100단위 반올림 | `_calc_grid_limit` | – |
| 성장/감소 status 임계 | `±1%` car_growth | compute_growth_analysis L2677 | – |
| `_half_change` 최소 길이 | 4 | L2532 | – |
| update-zone 쿨다운 | 5분 (300초) | server.py L246 | – |
| LLM timeout | 120초 | evaluate_simulation_llm L390 | – |
| LLM max_tokens | GPT 8000 / Claude 800 | L368 | – |

---

## 12. 클로닝 시 자주 막히는 함정 8가지

1. **시뮬레이션의 conv_rate는 "월별 minimum"**: 평균이 아니라 보수적으로 최저값 선택. 평균으로 바꾸면 추천대수가 1.5~2배로 부풀려진다.

2. **벤치마크 가중치는 `1/max(dist, 0.1)`**: dist=0(같은 점)일 때 분모 보호. 그냥 `1/dist`로 쓰면 시뮬레이션이 죽음.

3. **카니발리제이션은 `1 - (dist/radius)` overlap 가중**: 단순 카운트가 아님. 같은 점이면 `overlap=1.0` (해당 존 차량 전부 카니발), 0.5km(경계)면 `overlap=0`.

4. **권역 polygon은 prepared geometry로 캐싱**: 매번 union하면 1000개 좌표 contains 호출이 수십초. `prep()` 필수.

5. **강원도 zcode 매핑**: 환경부 API `51`, GeoJSON `32`, region1 컬럼 `'강원도' or '강원특별자치도'` — 셋 다 다름. `_REGION_ALIASES`, `EV_CHARGER_ZCODES`, `name_map` 세 곳에서 매핑.

6. **HTML이 자기완결형이므로 데이터 크기 = HTML 크기**: 권역당 20MB. 모바일에서는 lazy load 분리 필요.

7. **`car_count`는 BQ `carzone_info` 기준이 아니라 `car_info` 카운트**: zone_id로 join한 후 GROUP BY count. zone 메타의 정원이 아니라 현재 배치 차량.

8. **빌드 함수 3개에 root copy 로직 필수**: `_build_html` / `main` / `regenerate_from_cache` 어느 하나라도 빠지면 GitHub Pages 루트가 stale. (2026-05-12 강원도 충전 인프라 누락의 원인)

---

## 부록 A. cache 파일 생성 책임 매트릭스

| cache 파일 | `main()` | `update_demand` | `update_zone` | `regenerate_from_cache` |
|---|:---:|:---:|:---:|:---:|
| access.json | ✅ | ✅ | – | – |
| reservation.json | ✅ | ✅ | – | – |
| dtod.json | ✅ | ✅ | – | – |
| zones.json | ✅ | – | ✅ | – |
| profit.json | ✅ | – | ✅ | – |
| parking_contract.json | – | – | ✅ | – |
| timeline.json | ✅ | – | ✅ | – |
| gcar.json | ✅ | – | ✅ | – |
| closed.json | ✅ | – | ✅ | – |
| ev_chargers.json | – | – | ✅ | – |
| ev_virtual_zones.json | – | – | ✅ | – |
| ev_car_zones.json | – | – | ✅ | – |
| socar_supply.json | ✅ | – | ✅ | – |
| advance_util.json | ✅ | – | ✅ | – |
| weekly_trends.json | ✅ | – | – | – |
| supply_demand.json | ✅ | ✅ | – | – |
| gaps.json | ✅ | ✅ | – | – |
| dashboard_metrics.json | ✅ | – | ✅ | – |
| dashboard_region2.json | ✅ | – | ✅ | – |
| dashboard_region3.json | ✅ | – | – | – |
| `<team>/index.html` | ✅ | ✅ via `_build_html` | ✅ via `_build_html` | ✅ |
| `index.html` (root copy) | ✅ | ✅ | ✅ | ✅ |

---

## 부록 B. 외부 의존성

| 의존 | 버전 | 비고 |
|---|---|---|
| Python | 3.11+ | shapely 사용 |
| `shapely` | – | polygon prep + Point.contains |
| `bq` CLI | Google Cloud SDK | `gcloud auth login` + `socar-data` 프로젝트 |
| `openai` (LLM client) | – | `SOCAR_LLM_BASE` (사내 gateway) 사용 |
| `ngrok` | – | 듀얼 도메인 운영 |
| Leaflet | 1.9.4 | CDN |
| Leaflet.heat | 0.2.0 | CDN |
| Leaflet.markercluster | 1.5.3 | CDN |
| Pretendard | – | CDN (jsDelivr) |
| 환경부 충전기 API | B552584/EvCharger | 공공데이터포털 인증키 (코드 내) |
| Nominatim | – | 역지오코딩 (rate limit 1req/sec) |

---

_Last updated: 2026-05-12 — `update.py` 5,819 lines / `server.py` 484 lines 기준_
