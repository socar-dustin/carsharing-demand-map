#!/usr/bin/env python3
"""
경기도 카셰어링 잠재 수요 지도 - 주간 업데이트 스크립트
실행: python3 ~/carsharing_demand_map/update.py

index.html - 잠재 수요 지도 (앱 접속 + 예약 히트맵 + 존 마커 + Gap 분석 + 공급 분석)
"""

import json, math, os, subprocess, urllib.request, time, ssl, concurrent.futures
from datetime import datetime, timedelta
from shapely.geometry import Point, shape
from shapely.ops import unary_union
from shapely.prepared import prep

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
LAST_UPDATE_DEMAND_FILE = os.path.join(OUTPUT_DIR, '.last_update_demand')
LAST_UPDATE_ZONE_FILE = os.path.join(OUTPUT_DIR, '.last_update_zone')
NGROK_URL_FILE = os.path.join(OUTPUT_DIR, '.ngrok_url')

TODAY = datetime.now().strftime("%Y-%m-%d")

TEAM_CONFIG = {
    'seoul': {
        'name': '서울사업팀',
        'regions': ['서울특별시', '인천광역시'],
        'center': [37.5665, 126.978], 'zoom': 11,
        'bbox': {'lat': [37.35, 37.75], 'lng': [126.5, 127.2]},
    },
    'gyeonggi': {
        'name': '경기강원사업팀',
        'regions': ['경기도', '강원도'],
        'center': [37.5, 127.8], 'zoom': 8,
        'bbox': {'lat': [37.0, 38.6], 'lng': [126.4, 129.1]},
    },
    'chungcheong': {
        'name': '충청사업팀',
        'regions': ['충청남도', '충청북도', '대전광역시', '세종특별자치시'],
        'center': [36.5, 127.0], 'zoom': 10,
        'bbox': {'lat': [35.9, 37.1], 'lng': [126.3, 128.0]},
    },
    'gyeongbuk': {
        'name': '경북사업팀',
        'regions': ['경상북도', '대구광역시'],
        'center': [36.0, 128.6], 'zoom': 10,
        'bbox': {'lat': [35.5, 37.1], 'lng': [127.8, 130.0]},
    },
    'gyeongnam': {
        'name': '부울경사업팀',
        'regions': ['부산광역시', '울산광역시', '경상남도'],
        'center': [35.2, 129.0], 'zoom': 10,
        'bbox': {'lat': [34.5, 35.7], 'lng': [127.5, 129.5]},
    },
    'honam': {
        'name': '호남사업팀',
        'regions': ['전라남도', '전라북도', '광주광역시'],
        'center': [35.1, 126.9], 'zoom': 10,
        'bbox': {'lat': [34.2, 36.1], 'lng': [125.8, 127.5]},
    },
}

# ── 행정구역 polygon 기반 좌표 필터 ──
_PROVINCES_PATH = os.path.join(OUTPUT_DIR, 'korea_provinces.json')
_PROVINCES_GEOJSON = None
_TEAM_POLYGONS = {}  # {team_id: prepared polygon}

def _load_provinces():
    global _PROVINCES_GEOJSON
    if _PROVINCES_GEOJSON is None:
        with open(_PROVINCES_PATH) as f:
            _PROVINCES_GEOJSON = json.load(f)
    return _PROVINCES_GEOJSON

def _get_team_polygon(team_id):
    """팀의 regions에 해당하는 행정구역 polygon을 합쳐서 반환 (prepared)"""
    if team_id in _TEAM_POLYGONS:
        return _TEAM_POLYGONS[team_id]
    provinces = _load_provinces()
    regions = TEAM_CONFIG[team_id]['regions']
    # region name 매핑 (강원특별자치도 → 강원도 등)
    name_map = {
        '강원특별자치도': '강원도', '강원도': '강원도',
        '전북특별자치도': '전라북도',
        '세종특별자치시': '세종특별자치시',
    }
    target_names = set()
    for r in regions:
        target_names.add(name_map.get(r, r))
    polys = []
    for feat in provinces['features']:
        if feat['properties'].get('name', '') in target_names:
            polys.append(shape(feat['geometry']))
    if polys:
        merged = prep(unary_union(polys))
    else:
        merged = None
    _TEAM_POLYGONS[team_id] = merged
    return merged

def _calc_grid_limit(zone_count):
    """존 수 비례 히트맵 격자 수 (존당 약 3~4개 도트)"""
    limit = max(500, min(3000, zone_count * 3))
    return round(limit / 100) * 100  # 100단위 반올림

def filter_by_polygon(data, team_id, lat_key='lat', lng_key='lng'):
    """행정구역 polygon 기반으로 좌표 데이터 필터링"""
    poly = _get_team_polygon(team_id)
    if poly is None:
        return data
    return [r for r in data if poly.contains(Point(float(r[lng_key]), float(r[lat_key])))]


def _get_region1_by_polygon(lat, lng, team_id):
    """좌표가 속하는 시도(region1)를 polygon 기반으로 반환"""
    provinces = _load_provinces()
    regions = TEAM_CONFIG[team_id]['regions']
    name_map = {
        '강원특별자치도': '강원도', '강원도': '강원도',
        '전북특별자치도': '전라북도',
        '세종특별자치시': '세종특별자치시',
    }
    reverse_map = {}
    for r in regions:
        geojson_name = name_map.get(r, r)
        reverse_map[geojson_name] = r
    pt = Point(lng, lat)
    for feat in provinces['features']:
        fname = feat['properties'].get('name', '')
        if fname in reverse_map:
            if shape(feat['geometry']).contains(pt):
                return reverse_map[fname]
    return ''


_REGION_ALIASES = {
    '강원도': ['강원도', '강원특별자치도'],
    '전북특별자치도': ['전북특별자치도', '전라북도'],
}

def _expand_regions(regions):
    """region1 별칭 확장 (강원도 → 강원도+강원특별자치도)"""
    expanded = []
    for r in regions:
        expanded.extend(_REGION_ALIASES.get(r, [r]))
    return list(dict.fromkeys(expanded))  # 중복 제거, 순서 유지

def _region1_sql(team_id, alias=''):
    """team_id로부터 SQL IN 절 생성"""
    regions = _expand_regions(TEAM_CONFIG[team_id]['regions'])
    prefix = f'{alias}.' if alias else ''
    quoted = ', '.join(f"'{r}'" for r in regions)
    return f"{prefix}region1 IN ({quoted})"

def _region1_sql_col(team_id, col='region1'):
    """임의 컬럼명용 SQL IN 절"""
    regions = _expand_regions(TEAM_CONFIG[team_id]['regions'])
    quoted = ', '.join(f"'{r}'" for r in regions)
    return f"{col} IN ({quoted})"

def _team_bbox(team_id):
    return TEAM_CONFIG[team_id]['bbox']

def _team_cache_dir(team_id):
    d = os.path.join(OUTPUT_DIR, '.cache', team_id)
    os.makedirs(d, exist_ok=True)
    return d

def _team_output_dir(team_id):
    d = os.path.join(OUTPUT_DIR, team_id)
    os.makedirs(d, exist_ok=True)
    return d


def _read_last_update(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return '없음'
THREE_MONTHS_AGO = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
NEXT_DAY = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")


def run_bq(sql, max_rows=5000):
    """Execute BigQuery SQL via bq CLI and return rows."""
    cmd = ["bq", "query", "--use_legacy_sql=false", "--format=json", f"--max_rows={max_rows}", sql]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(f"BQ error: {result.stderr}")
    return json.loads(result.stdout)


def query_access(team_id='gyeonggi'):
    """앱 접속 위치 (광역 bounding box만, 서울/인천 제외는 Python 후처리)"""
    bb = _team_bbox(team_id)
    sql = f"""
    WITH markers_union AS (
      SELECT location.lat AS lat, location.lng AS lng
      FROM `socar-data.socar_server_3.GET_MARKERS_V2`
      WHERE timeMs >= TIMESTAMP('{THREE_MONTHS_AGO}', 'Asia/Seoul')
        AND timeMs < TIMESTAMP('{NEXT_DAY}', 'Asia/Seoul')
        AND location.lat BETWEEN {bb['lat'][0]} AND {bb['lat'][1]}
        AND location.lng BETWEEN {bb['lng'][0]} AND {bb['lng'][1]}
      UNION ALL
      SELECT location.lat AS lat, location.lng AS lng
      FROM `socar-data.socar_server_3.GET_MARKERS`
      WHERE timeMs >= TIMESTAMP('{THREE_MONTHS_AGO}', 'Asia/Seoul')
        AND timeMs < TIMESTAMP('{NEXT_DAY}', 'Asia/Seoul')
        AND location.lat BETWEEN {bb['lat'][0]} AND {bb['lat'][1]}
        AND location.lng BETWEEN {bb['lng'][0]} AND {bb['lng'][1]}
    )
    SELECT ROUND(lat,3) AS lat, ROUND(lng,3) AS lng, COUNT(*) AS access_count
    FROM markers_union WHERE lat IS NOT NULL AND lng IS NOT NULL
    GROUP BY 1,2 ORDER BY access_count DESC LIMIT 10000
    """
    cmd = ["bq", "query", "--use_legacy_sql=false", "--format=json", "--max_rows=10000", sql]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(f"BQ error: {result.stderr}")
    rows = json.loads(result.stdout)
    for r in rows:
        r['lat'] = float(r['lat'])
        r['lng'] = float(r['lng'])
        r['access_count'] = int(r['access_count'])
    return rows


def query_reservation(team_id='gyeonggi'):
    """예약 생성 위치 (광역 bounding box만, 서울/인천 제외는 Python 후처리)"""
    region_filter = _region1_sql(team_id)
    bb = _team_bbox(team_id)
    sql = f"""
    SELECT ROUND(reservation_created_lat,3) AS lat, ROUND(reservation_created_lng,3) AS lng,
           COUNT(*) AS reservation_count
    FROM `socar-data.soda_store.reservation_v2`
    WHERE date >= '{THREE_MONTHS_AGO}' AND date <= '{TODAY}'
      AND {region_filter} AND state = 3 AND member_imaginary IN (0,9)
      AND reservation_created_lat IS NOT NULL AND reservation_created_lng IS NOT NULL
      AND reservation_created_lat BETWEEN {bb['lat'][0]} AND {bb['lat'][1]}
      AND reservation_created_lng BETWEEN {bb['lng'][0]} AND {bb['lng'][1]}
    GROUP BY 1,2 ORDER BY reservation_count DESC LIMIT 10000
    """
    cmd = ["bq", "query", "--use_legacy_sql=false", "--format=json", "--max_rows=10000", sql]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(f"BQ error: {result.stderr}")
    rows = json.loads(result.stdout)
    for r in rows:
        r['lat'] = float(r['lat'])
        r['lng'] = float(r['lng'])
        r['reservation_count'] = int(r['reservation_count'])
    return rows



def query_reservation_by_zone_region(team_id='gyeonggi'):
    """지역 존 소속 차량의 실제 예약 건수 (예약 생성 위치가 아닌, 차량 소속 지역 기준)"""
    cz_region_filter = _region1_sql(team_id, 'cz')
    sql = f"""
    SELECT cz.region2, COUNT(*) AS reservation_count
    FROM `socar-data.soda_store.reservation_v2` r
    JOIN `socar-data.tianjin_replica.car_info` c ON r.car_id = c.id
    JOIN `socar-data.tianjin_replica.carzone_info` cz ON c.zone_id = cz.id
    WHERE r.date >= '{THREE_MONTHS_AGO}' AND r.date <= '{TODAY}'
      AND {cz_region_filter}
      AND cz.state = 1 AND cz.imaginary IN (0,3,5) AND cz.visibility = 1
      AND r.state = 3
      AND r.member_imaginary IN (0,9)
    GROUP BY cz.region2
    ORDER BY reservation_count DESC
    """
    return run_bq(sql)


def query_dtod(team_id='gyeonggi'):
    """부름 호출 위치 (팀 영역)"""
    bb = _team_bbox(team_id)
    sql = f"""
    SELECT ROUND(d.start_lat, 4) AS lat, ROUND(d.start_lng, 4) AS lng,
           COUNT(*) AS call_count
    FROM `socar-data.tianjin_replica.reservation_dtod_info` d
    JOIN `socar-data.soda_store.reservation_v2` r ON r.reservation_id = d.reservation_id
    WHERE d.created_at >= TIMESTAMP('{THREE_MONTHS_AGO}', 'Asia/Seoul')
      AND d.created_at < TIMESTAMP('{NEXT_DAY}', 'Asia/Seoul')
      AND r.state = 3
      AND d.start_lat BETWEEN {bb['lat'][0]} AND {bb['lat'][1]}
      AND d.start_lng BETWEEN {bb['lng'][0]} AND {bb['lng'][1]}
      AND d.start_lat IS NOT NULL AND d.start_lng IS NOT NULL
    GROUP BY 1, 2
    HAVING call_count >= 12
    ORDER BY call_count DESC
    """
    cmd = ["bq", "query", "--use_legacy_sql=false", "--format=json", "--max_rows=10000", sql]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(f"BQ error: {result.stderr}")
    rows = json.loads(result.stdout)
    for r in rows:
        r['lat'] = float(r['lat'])
        r['lng'] = float(r['lng'])
        r['call_count'] = int(r['call_count'])
    return rows


SOCAR_LLM_KEY = os.environ.get('SOCAR_LLM_KEY', '')
SOCAR_LLM_BASE = 'https://litellm.ai.socarcorp.co.kr/v1'


def query_d2d_destinations(zone_id):
    """해당 존에서 부름으로 호출된 위치 (최근 3개월, 예약 시점 존 기준)"""
    sql = f"""
    SELECT d.start_lat AS lat, d.start_lng AS lng, d.start_address1 AS address,
           d.way, COUNT(*) AS cnt
    FROM `socar-data.tianjin_replica.reservation_dtod_info` d
    JOIN `socar-data.soda_store.reservation_v2` r ON r.reservation_id = d.reservation_id
    WHERE r.zone_id = {zone_id}
      AND r.state = 3 AND r.date >= '{THREE_MONTHS_AGO}'
      AND d.start_lat IS NOT NULL AND d.start_lat != 0
    GROUP BY 1, 2, 3, 4
    ORDER BY cnt DESC
    LIMIT 200
    """
    cmd = ["bq", "query", "--use_legacy_sql=false", "--format=json", "--max_rows=200", sql]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(f"BQ error: {result.stderr[:300]}")
    rows = json.loads(result.stdout)
    destinations = []
    for r in rows:
        destinations.append({
            'lat': float(r.get('lat', 0)),
            'lng': float(r.get('lng', 0)),
            'address': r.get('address', ''),
            'way': r.get('way', ''),
            'cnt': int(r.get('cnt', 0)),
        })
    return {'zone_id': zone_id, 'destinations': destinations, 'total': sum(d['cnt'] for d in destinations)}

def evaluate_simulation_llm(sim_data, team_id='gyeonggi'):
    """GPT 5.2 + Claude 3.5 Sonnet 으로 존 개설 시뮬레이션 평가"""
    from openai import OpenAI
    client = OpenAI(base_url=SOCAR_LLM_BASE, api_key=SOCAR_LLM_KEY)
    team_name = TEAM_CONFIG[team_id]['name']

    prompt = f"""당신은 쏘카(SOCAR) 카셰어링 존 개설 전략 분석가입니다.
아래 시뮬레이션 데이터를 분석하세요.

[위치] {sim_data.get('region2','')} {sim_data.get('region3','')}
[수요 등급] {sim_data.get('demand_grade','?')} ({team_name} 접속 격자 상위 {100-sim_data.get('demand_percentile',0):.0f}%)
[반경 500m 주간 수요] 접속 {sim_data['weekly_access']:,} / 예약 {sim_data['weekly_res']:,} / 부름 {sim_data['weekly_dtod']:,}
[전환율] {sim_data['conv_rate']*100:.2f}% ({sim_data['conv_level']}) — {sim_data.get('region2','')} 내 {sim_data.get('conv_total_r3',0)}개 지역 중 {sim_data.get('conv_rank','?')}위 (평균 {sim_data.get('r2_avg_conv',0):.2f}%, 최고 {sim_data.get('r2_max_conv',0):.2f}%, 최저 {sim_data.get('r2_min_conv',0):.2f}%)
[예상 주간 예약] {sim_data['est_weekly_res']}건 (접속기반 {sim_data.get('base_res',0)} + 부름전환 {sim_data.get('dtod_additional',0)})
[인근 벤치마크 ({sim_data['bench_zone_count']}개 존)] 대당매출 {sim_data['est_rev_per_car']:,}원/4주, 대당GP {sim_data['est_gp_per_car']:,}원/4주, 가동률 {sim_data['avg_util']:.1f}%
[매출 추정] 건당매출 {sim_data['revenue_per_res']:,}원, 추정 월매출 {sim_data['est_monthly_revenue']:,}원
[대당 목표매출] 1,600,000원/4주
[수요 기반 적정대수] {sim_data['raw_recommended_cars']}대
[카니발리제이션] 반경 500m 내 기존 {sim_data['nearby_zone_count']}개 존 {sim_data['nearby_total_cars']}대, 카니발 차감 -{sim_data['cannibal_cars']}대
[추천 공급대수] {sim_data['recommended_cars']}대
[최근접 존] {sim_data.get('nearest_zone','-')} ({sim_data.get('nearest_zone_dist_km','-')}km)
[과거 존 이력] {len(sim_data.get('hist_zones',[]))}건"""

    if sim_data.get('hist_zones'):
        for hz in sim_data['hist_zones']:
            prompt += f"\n  - {hz['zone_name']}: {hz['first_date']}~{hz['last_date']} ({hz['operation_days']}일 운영), 대당매출 {hz['revenue_per_car_28d']:,}원, GP {hz['gp_per_car_28d']:,}원, 가동률 {hz['avg_utilization']}%"

    prompt += """

접속량과 지역 전환율을 바탕으로 계산된 주 예약 추정 수치를 활용하여 해당 지역의 수요 포텐셜을 평가하고, 대당 매출 160만원을 목표로 건당 매출 × 주 예약 건으로 계산되는 수요 기반 적정 공급 대수, 카니발리제이션이 보정된 추천 공급대수 수치를 신뢰할 수 있는지 판단하세요. 신뢰할 수 없다면 어떤 이유에서이며 그 근거를 상세하게 기술하세요. 마지막으로 인근(반경 500m) 존의 실적과 해당 지역에서 과거 운영했던 존의 실적 등 그 외 지표를 적극 활용하여 최종 개설 여부 판단을 내려주세요.

다음 형식으로 작성하세요. 첫 줄은 반드시 추천 여부로 시작하고, 각 항목 사이에 빈 줄을 넣으세요:
개설 추천 O 또는 개설 비추천 X

[1] 수요 식별: (1~2문장)

[2] 추천 공급 대수 검증: (1~2문장)

[3] 최종 코멘트: (1~2문장)"""

    def call_model(model):
        try:
            max_tok = 8000 if 'gpt' in model else 800
            resp = client.chat.completions.create(
                model=model,
                messages=[{'role': 'user', 'content': prompt}],
                max_tokens=max_tok,
                temperature=0.3,
            )
            content = resp.choices[0].message.content
            return content.strip() if content else '응답 없음 (reasoning 모델 토큰 부족)'
        except Exception as e:
            return f'평가 실패: {e}'

    # 두 모델 병렬 호출
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        gpt_future = pool.submit(call_model, 'dev/gpt-5.2-chat')
        claude_future = pool.submit(call_model, 'prod/claude-3.5-sonnet')
        try:
            gpt_result = gpt_future.result(timeout=120)
        except Exception as e:
            gpt_result = f'평가 시간 초과'
        try:
            claude_result = claude_future.result(timeout=120)
        except Exception as e:
            claude_result = f'평가 시간 초과'

    return {'gpt': gpt_result, 'claude': claude_result}


def simulate_zone(lat, lng, radius_km=0.5, team_id='gyeonggi'):
    """존 개설 시뮬레이션 — 실시간 BQ 쿼리로 정확한 수요 산출"""
    team_name = TEAM_CONFIG[team_id]['name']
    # 위도/경도 → 반경 변환 (대략)
    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * abs(math.cos(math.radians(lat))))

    lat_min, lat_max = lat - dlat, lat + dlat
    lng_min, lng_max = lng - dlng, lng + dlng

    num_weeks = 90 / 7

    # 1) 반경 내 앱 접속수 (LIMIT 없이 정확한 수)
    access_sql = f"""
    WITH markers AS (
      SELECT location.lat AS lat, location.lng AS lng
      FROM `socar-data.socar_server_3.GET_MARKERS_V2`
      WHERE timeMs >= TIMESTAMP('{THREE_MONTHS_AGO}', 'Asia/Seoul')
        AND timeMs < TIMESTAMP('{NEXT_DAY}', 'Asia/Seoul')
        AND location.lat BETWEEN {lat_min} AND {lat_max}
        AND location.lng BETWEEN {lng_min} AND {lng_max}
      UNION ALL
      SELECT location.lat AS lat, location.lng AS lng
      FROM `socar-data.socar_server_3.GET_MARKERS`
      WHERE timeMs >= TIMESTAMP('{THREE_MONTHS_AGO}', 'Asia/Seoul')
        AND timeMs < TIMESTAMP('{NEXT_DAY}', 'Asia/Seoul')
        AND location.lat BETWEEN {lat_min} AND {lat_max}
        AND location.lng BETWEEN {lng_min} AND {lng_max}
    )
    SELECT COUNT(*) AS total_access FROM markers
    WHERE lat IS NOT NULL AND lng IS NOT NULL
    """

    # 2) 반경 내 예약 생성수
    res_sql = f"""
    SELECT COUNT(*) AS total_res
    FROM `socar-data.soda_store.reservation_v2`
    WHERE date >= '{THREE_MONTHS_AGO}' AND date <= '{TODAY}'
      AND state = 3 AND member_imaginary IN (0,9)
      AND reservation_created_lat BETWEEN {lat_min} AND {lat_max}
      AND reservation_created_lng BETWEEN {lng_min} AND {lng_max}
    """

    # 3) 반경 내 부름 호출수
    dtod_sql = f"""
    SELECT COUNT(*) AS total_dtod
    FROM `socar-data.tianjin_replica.reservation_dtod_info`
    WHERE created_at >= TIMESTAMP('{THREE_MONTHS_AGO}', 'Asia/Seoul')
      AND created_at < TIMESTAMP('{NEXT_DAY}', 'Asia/Seoul')
      AND start_lat BETWEEN {lat_min} AND {lat_max}
      AND start_lng BETWEEN {lng_min} AND {lng_max}
    """

    # 4) 해당 지역의 region2/3 파악 (가장 가까운 존 기준)
    zones = _load_cache("zones", team_id) or []
    nearest_zone = None
    nearest_dist = float('inf')
    for z in zones:
        d = ((float(z['lat']) - lat)**2 + (float(z['lng']) - lng)**2) ** 0.5
        if d < nearest_dist:
            nearest_dist = d
            nearest_zone = z

    region2 = nearest_zone.get('region2', '') if nearest_zone else ''
    region3 = nearest_zone.get('region3', '') if nearest_zone else ''

    # 5) region3 / region2 전환율 (월별 minimum 기준)
    # 월별 예약건수 + 건당 이용시간
    conv_sql = f"""
    SELECT cz.region2, cz.region3,
           FORMAT_DATE('%Y-%m', r.date) AS month,
           COUNT(*) AS res_cnt,
           COUNT(DISTINCT c.id) AS car_cnt,
           AVG(TIMESTAMP_DIFF(r.end_at_kst, r.start_at_kst, MINUTE) / 60.0) AS avg_hours_per_res
    FROM `socar-data.soda_store.reservation_v2` r
    JOIN `socar-data.tianjin_replica.car_info` c ON r.car_id = c.id
    JOIN `socar-data.tianjin_replica.carzone_info` cz ON c.zone_id = cz.id
    WHERE r.date >= '{THREE_MONTHS_AGO}' AND r.date <= '{TODAY}'
      AND {_region1_sql(team_id, 'cz')} AND cz.state = 1
      AND cz.imaginary IN (0,3,5) AND cz.visibility = 1
      AND r.state = 3 AND r.member_imaginary IN (0,9)
      AND cz.region2 = '{region2}'
    GROUP BY 1, 2, 3
    ORDER BY cz.region3, month
    """

    # BQ 쿼리 실행
    def bq_single(sql):
        cmd = ["bq", "query", "--use_legacy_sql=false", "--format=json", "--max_rows=100", sql]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            err = r.stderr[:300] or r.stdout[:300]
            raise RuntimeError(f"BQ error: {err}")
        # bq가 returncode=0이지만 에러를 stdout에 출력하는 경우
        out = r.stdout.strip()
        if out.startswith('Error') or out.startswith('Syntax'):
            raise RuntimeError(f"BQ error: {out[:300]}")
        return json.loads(out)

    # 6) 반경 300m 내 과거 존 이력 (폐존 state=0)
    hist_dlat = 0.3 / 111.0
    hist_dlng = 0.3 / (111.0 * abs(math.cos(math.radians(lat))))
    hist_sql = f"""
    WITH nearby_zones AS (
      SELECT z.legacy_zone_id AS zone_id, cz.zone_name, cz.lat, cz.lng, z.type AS zone_type
      FROM `socar-data.socar_zone.zone` z
      JOIN `socar-data.tianjin_replica.carzone_info` cz ON cz.id = z.legacy_zone_id
      WHERE {_region1_sql_col(team_id, 'z.region_1')}
        AND z.type IN ('ZONTP_NORMAL','ZONTP_D2D_STATION','ZONTP_D2D_PRIORITY','ZONTP_D2D_CALL')
        AND cz.lat BETWEEN {lat - hist_dlat} AND {lat + hist_dlat}
        AND cz.lng BETWEEN {lng - hist_dlng} AND {lng + hist_dlng}
    ),
    inactive_zones AS (
      SELECT nz.zone_id, nz.zone_name, nz.lat, nz.lng, nz.zone_type
      FROM nearby_zones nz
      LEFT JOIN `socar-data.socar_biz_profit.profit_socar_car_daily` p
        ON nz.zone_id = p.zone_id AND p.date = CURRENT_DATE() - 1
      GROUP BY 1, 2, 3, 4, 5
      HAVING IFNULL(SUM(p.opr_day), 0) = 0
    )
    SELECT iz.zone_id, iz.zone_name, iz.lat, iz.lng, iz.zone_type,
           MIN(p.date) AS first_date, MAX(p.date) AS last_date,
           COUNT(DISTINCT p.date) AS operation_days,
           COUNT(DISTINCT p.car_id) AS total_cars_ever,
           ROUND(SAFE_DIVIDE(SUM(p.revenue) * 28, NULLIF(SUM(p.opr_day), 0))) AS revenue_per_car_28d,
           ROUND(SAFE_DIVIDE(SUM(p.profit) * 28, NULLIF(SUM(p.opr_day), 0))) AS gp_per_car_28d,
           ROUND(SAFE_DIVIDE(SUM(o.op_min), SUM(o.dp_min) - SUM(o.bl_min)) * 100, 1) AS avg_utilization,
           SUM(p.nuse) AS total_nuse,
           ROUND(SUM(p.revenue) / NULLIF(SUM(p.nuse), 0), 0) AS revenue_per_res,
           ROUND(SUM(p.nuse) * 1.0 / NULLIF(COUNT(DISTINCT p.date), 0) * 30, 1) AS monthly_avg_res
    FROM inactive_zones iz
    JOIN `socar-data.socar_biz_profit.profit_socar_car_daily` p
      ON p.zone_id = iz.zone_id
    LEFT JOIN `socar-data.socar_biz.operation_per_car_daily_v2` o
      ON o.zone_id = iz.zone_id AND o.date = p.date
    WHERE p.car_sharing_type IN ('socar', 'zplus')
    GROUP BY 1, 2, 3, 4, 5
    """

    access_rows = bq_single(access_sql)
    res_rows = bq_single(res_sql)
    dtod_rows = bq_single(dtod_sql)

    total_access_90d = int(access_rows[0]['total_access']) if access_rows else 0
    total_res_90d = int(res_rows[0]['total_res']) if res_rows else 0
    total_dtod_90d = int(dtod_rows[0]['total_dtod']) if dtod_rows else 0

    weekly_access = round(total_access_90d / num_weeks)
    weekly_res = round(total_res_90d / num_weeks)
    weekly_dtod = round(total_dtod_90d / num_weeks)

    # 전환율 계산: 캐시된 접속 데이터에서 region별 접속수 집계
    cached_access = _load_cache("access", team_id) or []
    # region별 접속수: 각 접속 좌표를 가장 가까운 존의 region에 배정
    region_access = {}
    for z in zones:
        r2 = z.get('region2', '')
        r3 = z.get('region3', '')
        if r2 not in region_access:
            region_access[r2] = {'total': 0, 'by_r3': {}}
        if r3 and r3 not in region_access[r2]['by_r3']:
            region_access[r2]['by_r3'][r3] = 0
    for a in cached_access:
        alat, alng, cnt = float(a['lat']), float(a['lng']), int(a['access_count'])
        best_d, best_r2, best_r3 = float('inf'), '', ''
        for z in zones:
            d = (float(z['lat']) - alat)**2 + (float(z['lng']) - alng)**2
            if d < best_d:
                best_d = d
                best_r2 = z.get('region2', '')
                best_r3 = z.get('region3', '')
        if best_r2:
            if best_r2 not in region_access:
                region_access[best_r2] = {'total': 0, 'by_r3': {}}
            region_access[best_r2]['total'] += cnt
            if best_r3:
                region_access[best_r2]['by_r3'][best_r3] = region_access[best_r2]['by_r3'].get(best_r3, 0) + cnt

    # 월별 전환율 → minimum 사용 (region3 → region2 fallback)
    conv_rows = bq_single(conv_sql)

    # 월별 접속수 추정: 전체 접속을 3등분 (간이)
    num_months = 3
    region2_access_total = region_access.get(region2, {}).get('total', 0)
    r3_access_total = region_access.get(region2, {}).get('by_r3', {}).get(region3, 0)
    monthly_r2_access = region2_access_total / num_months if region2_access_total > 0 else 0
    monthly_r3_access = r3_access_total / num_months if r3_access_total > 0 else 0

    # region3 월별 전환율
    r3_monthly_convs = []
    if monthly_r3_access > 0:
        months_r3 = {}
        for cr in conv_rows:
            if cr.get('region3', '') == region3:
                m = cr.get('month', '')
                months_r3[m] = months_r3.get(m, 0) + int(cr.get('res_cnt', 0))
        for m, res in months_r3.items():
            if res > 0:
                r3_monthly_convs.append(res / monthly_r3_access)

    # region2 월별 전환율
    r2_monthly_convs = []
    if monthly_r2_access > 0:
        months_r2 = {}
        for cr in conv_rows:
            m = cr.get('month', '')
            months_r2[m] = months_r2.get(m, 0) + int(cr.get('res_cnt', 0))
        for m, res in months_r2.items():
            if res > 0:
                r2_monthly_convs.append(res / monthly_r2_access)

    # region3 월별 최저 전환율 우선, 없으면 region2 월별 최저
    if r3_monthly_convs:
        conv_rate = min(r3_monthly_convs)
        conv_level = f'{region3} (월별 min)'
    elif r2_monthly_convs:
        conv_rate = min(r2_monthly_convs)
        conv_level = f'{region2} (월별 min)'
    else:
        total_gg_access = sum(v.get('total', 0) for v in region_access.values())
        total_gg_res = sum(int(cr.get('res_cnt', 0)) for cr in conv_rows)
        conv_rate = total_gg_res / total_gg_access if total_gg_access > 0 else 0
        conv_level = team_name

    # region2 내 다른 region3들과의 전환율 비교 컨텍스트
    r3_conv_comparison = []
    r3_all_in_r2 = {}  # {region3: total_res}
    for cr in conv_rows:
        r3_name = cr.get('region3', '')
        if r3_name:
            r3_all_in_r2[r3_name] = r3_all_in_r2.get(r3_name, 0) + int(cr.get('res_cnt', 0))
    r3_by_r2 = region_access.get(region2, {}).get('by_r3', {})
    for r3_name, r3_res in r3_all_in_r2.items():
        r3_acc = r3_by_r2.get(r3_name, 0)
        if r3_acc > 0:
            r3_conv_comparison.append({'region3': r3_name, 'conv': round(r3_res / r3_acc * 100, 3)})
    r3_conv_comparison.sort(key=lambda x: -x['conv'])
    # 해당 region3의 순위
    conv_rank = None
    if r3_conv_comparison:
        for i, c in enumerate(r3_conv_comparison):
            if c['region3'] == region3:
                conv_rank = i + 1
                break
    r2_avg_conv = sum(c['conv'] for c in r3_conv_comparison) / len(r3_conv_comparison) if r3_conv_comparison else 0
    r2_max_conv = r3_conv_comparison[0]['conv'] if r3_conv_comparison else 0
    r2_min_conv = r3_conv_comparison[-1]['conv'] if r3_conv_comparison else 0

    # 건당 평균 이용시간 (region3 → region2 → 경기도 fallback)
    r3_hours = None
    r2_hours_sum, r2_hours_cnt = 0.0, 0
    for cr in conv_rows:
        h = float(cr.get('avg_hours_per_res', 0) or 0)
        if h > 0:
            cnt = int(cr.get('res_cnt', 1))
            r2_hours_sum += h * cnt
            r2_hours_cnt += cnt
            if cr.get('region3', '') == region3:
                r3_hours = h

    if r3_hours and r3_hours > 0:
        avg_hours_per_res = round(r3_hours, 1)
        hours_level = region3
    elif r2_hours_cnt > 0:
        avg_hours_per_res = round(r2_hours_sum / r2_hours_cnt, 1)
        hours_level = region2
    else:
        avg_hours_per_res = 8.0
        hours_level = f'{team_name} (기본값)'

    # 예상 주간 예약 (접속 × 전환율 + 부름 전환)
    base_res = round(weekly_access * conv_rate)
    dtod_conversion = 0.5  # 부름 호출 중 존 개설 시 일반 예약으로 전환되는 비율
    dtod_additional = round(weekly_dtod * dtod_conversion)
    est_weekly_res = base_res + dtod_additional

    # 인근 존 실적 + 주차비 (캐시에서)
    profit_data = _load_cache("profit", team_id) or {}
    if profit_data:
        profit_data = {int(k): v for k, v in profit_data.items()}
    parking_contract = _load_cache("parking_contract", team_id) or {}
    if parking_contract:
        parking_contract = {int(k): v for k, v in parking_contract.items()}

    bench_zones = []
    bench_radius = 3.0
    for z in zones:
        zd = ((float(z['lat']) - lat)**2 + (float(z['lng']) - lng)**2) ** 0.5 * 111
        if zd <= bench_radius and int(z.get('car_count', 0)) > 0:
            zid = int(z['zone_id'])
            p = profit_data.get(zid, {})
            pc = parking_contract.get(zid, {})
            rev = p.get('revenue_per_car_28d', 0)
            gp = p.get('gp_per_car_28d', 0)
            util = p.get('utilization_rate', 0)
            rpr = p.get('revenue_per_res', 0)
            parking_cost = pc.get('price_per_car', 0)
            if rev > 0:
                bench_zones.append({'dist': zd, 'rev': rev, 'gp': gp, 'util': util, 'rpr': rpr,
                                    'name': z.get('zone_name', ''), 'cars': int(z['car_count']),
                                    'parking_cost': parking_cost, 'settlement_type': pc.get('settlement_type', '')})

    bench_zones.sort(key=lambda x: x['dist'])

    # 가중평균 실적
    w_rev, w_gp, w_util, w_rpr, w_total = 0, 0, 0, 0, 0
    for bz in bench_zones:
        w = 1 / max(bz['dist'], 0.1)
        w_rev += bz['rev'] * w
        w_gp += bz['gp'] * w
        w_util += bz['util'] * w
        if bz['rpr'] > 0:
            w_rpr += bz['rpr'] * w
        w_total += w

    est_rev_per_car = round(w_rev / w_total) if w_total > 0 else 0
    est_gp_per_car = round(w_gp / w_total) if w_total > 0 else 0
    avg_util = w_util / w_total if w_total > 0 else 40

    # 건당 매출: SUM(revenue) / SUM(nuse) 기반 가중평균
    TARGET_REV_PER_CAR = 1600000  # 대당 매출 목표 160만원
    revenue_per_res = round(w_rpr / w_total) if w_total > 0 and w_rpr > 0 else 0

    # 추천 공급대수: 총 매출 추정 → 대당 160만원 기준 역산
    est_monthly_revenue = est_weekly_res * 4 * revenue_per_res
    if TARGET_REV_PER_CAR > 0 and revenue_per_res > 0:
        cars_needed = est_monthly_revenue / TARGET_REV_PER_CAR
    else:
        cars_needed = 0
    raw_recommended_cars = max(0, math.floor(cars_needed))

    # 카니발리제이션 보정: 반경 1km 내 기존 공급 차량 (거리 가중)
    cannibal_cars = 0.0
    nearby_total_cars = 0
    nearby_zone_count = 0
    for z in zones:
        zd = ((float(z['lat']) - lat)**2 + (float(z['lng']) - lng)**2) ** 0.5 * 111
        car_cnt = int(z.get('car_count', 0))
        if zd <= radius_km and car_cnt > 0:
            overlap = 1 - (zd / radius_km)  # 가까울수록 겹침 큼
            cannibal_cars += car_cnt * overlap
            nearby_total_cars += car_cnt
            nearby_zone_count += 1

    cannibal_cars = round(cannibal_cars, 1)
    recommended_cars = max(0, math.floor(raw_recommended_cars - cannibal_cars))

    is_recommend = recommended_cars >= 1

    # 수요 등급: 경기도 전체 접속 격자 대비 백분위 기반
    all_access_counts = sorted([int(a['access_count']) for a in cached_access if int(a['access_count']) > 0])
    if all_access_counts and total_access_90d > 0:
        rank = sum(1 for x in all_access_counts if x <= total_access_90d)
        percentile = rank / len(all_access_counts) * 100
        if percentile >= 95:
            demand_grade = 'S'
        elif percentile >= 80:
            demand_grade = 'A'
        elif percentile >= 50:
            demand_grade = 'B'
        else:
            demand_grade = 'F'
    else:
        percentile = 0
        demand_grade = 'F'

    # 과거 존 이력 조회
    hist_rows = bq_single(hist_sql)
    hist_zones = []
    for h in hist_rows:
        is_d2d_call = h.get('zone_type', '') == 'ZONTP_D2D_CALL'
        hist_zones.append({
            'zone_id': int(h['zone_id']),
            'zone_name': h.get('zone_name', ''),
            'zone_type': h.get('zone_type', ''),
            'is_d2d_call': is_d2d_call,
            'first_date': h.get('first_date', ''),
            'last_date': h.get('last_date', ''),
            'operation_days': int(h.get('operation_days', 0)),
            'total_cars_ever': int(h.get('total_cars_ever', 0)),
            'revenue_per_car_28d': int(float(h.get('revenue_per_car_28d', 0) or 0)),
            'gp_per_car_28d': int(float(h.get('gp_per_car_28d', 0) or 0)),
            'avg_utilization': float(h.get('avg_utilization', 0) or 0),
            'monthly_avg_res': float(h.get('monthly_avg_res', 0) or 0),
            'revenue_per_res': int(float(h.get('revenue_per_res', 0) or 0)),
        })

    # 과거 존 실적이 있으면 대당매출/추천대수에 반영 (가중 보정)
    if hist_zones:
        # 과거 실적의 가중평균 대당매출
        hist_rev_sum = sum(hz['revenue_per_car_28d'] for hz in hist_zones)
        hist_rev_avg = hist_rev_sum / len(hist_zones) if hist_zones else 0
        # 현재 추정과 과거 실적을 7:3 비중으로 블렌딩
        if hist_rev_avg > 0 and est_rev_per_car > 0:
            est_rev_per_car = round(est_rev_per_car * 0.7 + hist_rev_avg * 0.3)
        elif hist_rev_avg > 0:
            est_rev_per_car = round(hist_rev_avg)
        # 추천대수는 현재 수요 기반으로만 산출 (과거 차량수 블렌딩 제거)
        is_recommend = recommended_cars >= 1

    # ── 주차비 분석 ──
    INFLATION_RATE = 0.03  # 연 3% CPI
    def round_1000(v):
        """천 단위 반올림"""
        return round(v / 1000) * 1000

    # 1) 인근 벤치마크 주차비
    bench_parking_zones = [bz for bz in bench_zones if bz.get('parking_cost', 0) > 0]
    if bench_parking_zones:
        w_park, w_park_total = 0, 0
        for bz in bench_parking_zones:
            w = 1 / max(bz['dist'], 0.1)
            w_park += bz['parking_cost'] * w
            w_park_total += w
        bench_avg_parking = round_1000(w_park / w_park_total) if w_park_total > 0 else 0
    else:
        bench_avg_parking = 0

    # 2) 과거 존 주차비 (물가보정)
    hist_parking_costs = []
    for hz in hist_zones:
        hpc = parking_contract.get(hz['zone_id'], {}).get('price_per_car', 0)
        if hpc > 0:
            # 운영 중간시점 ~ 현재까지 연수
            try:
                mid_date = hz.get('first_date', '')
                last_date = hz.get('last_date', '')
                from datetime import datetime as _dt
                d1 = _dt.strptime(str(mid_date), '%Y-%m-%d')
                d2 = _dt.strptime(str(last_date), '%Y-%m-%d')
                mid = d1 + (d2 - d1) / 2
                years = (_dt.now() - mid).days / 365.25
            except Exception:
                years = 2
            adjusted = round_1000(hpc * (1 + INFLATION_RATE) ** years)
            hist_parking_costs.append({'zone_name': hz['zone_name'], 'original': hpc, 'adjusted': adjusted, 'years': round(years, 1)})
    hist_avg_parking = round_1000(sum(h['adjusted'] for h in hist_parking_costs) / len(hist_parking_costs)) if hist_parking_costs else 0

    # 3) 추천 주차비 range
    if bench_avg_parking > 0 and hist_avg_parking > 0:
        parking_range_low = min(bench_avg_parking, hist_avg_parking)
        parking_range_high = max(bench_avg_parking, hist_avg_parking)
    elif bench_avg_parking > 0:
        parking_range_low = round_1000(bench_avg_parking * 0.85)
        parking_range_high = round_1000(bench_avg_parking * 1.15)
    elif hist_avg_parking > 0:
        parking_range_low = round_1000(hist_avg_parking * 0.85)
        parking_range_high = round_1000(hist_avg_parking * 1.15)
    else:
        parking_range_low = 0
        parking_range_high = 0
    parking_mid = round_1000((parking_range_low + parking_range_high) / 2) if (parking_range_low + parking_range_high) > 0 else 0

    # 4) GPM 시뮬레이션 (5 시나리오: -25%, -10%, 기준, +10%, +25%)
    gpm_scenarios = []
    if parking_mid > 0 and est_rev_per_car > 0:
        for pct in [-0.25, -0.10, 0, 0.10, 0.25]:
            pc = max(0, round_1000(parking_mid * (1 + pct)))
            # GP 보정: 벤치마크 GP에서 주차비 차이만큼 조정
            parking_diff = pc - bench_avg_parking if bench_avg_parking > 0 else 0
            gp = est_gp_per_car - parking_diff
            gpm = round(gp / est_rev_per_car * 100, 1) if est_rev_per_car > 0 else 0
            gpm_scenarios.append({'parking_cost': pc, 'gp_per_car': round(gp), 'gpm': gpm, 'is_mid': pct == 0})

    return {
        'lat': lat, 'lng': lng, 'radius_km': radius_km,
        'region2': region2, 'region3': region3,
        'demand_grade': demand_grade,
        'demand_percentile': round(percentile, 1),
        'weekly_access': weekly_access,
        'weekly_res': weekly_res,
        'weekly_dtod': weekly_dtod,
        'total_access_90d': total_access_90d,
        'total_res_90d': total_res_90d,
        'conv_rate': round(conv_rate, 6),
        'conv_level': conv_level,
        'conv_rank': conv_rank,
        'conv_total_r3': len(r3_conv_comparison),
        'r2_avg_conv': round(r2_avg_conv, 3),
        'r2_max_conv': round(r2_max_conv, 3),
        'r2_min_conv': round(r2_min_conv, 3),
        'avg_hours_per_res': avg_hours_per_res,
        'hours_level': hours_level,
        'base_res': base_res,
        'dtod_additional': dtod_additional,
        'est_weekly_res': est_weekly_res,
        'bench_zone_count': len(bench_zones),
        'est_rev_per_car': est_rev_per_car,
        'est_gp_per_car': est_gp_per_car,
        'avg_util': round(avg_util, 1),
        'revenue_per_res': revenue_per_res,
        'est_monthly_revenue': est_monthly_revenue,
        'raw_recommended_cars': raw_recommended_cars,
        'cannibal_cars': cannibal_cars,
        'nearby_total_cars': nearby_total_cars,
        'nearby_zone_count': nearby_zone_count,
        'recommended_cars': recommended_cars,
        'is_recommend': is_recommend,
        'nearest_zone': nearest_zone.get('zone_name', '') if nearest_zone else '',
        'nearest_zone_dist_km': round(nearest_dist * 111, 1) if nearest_zone else None,
        'hist_zones': hist_zones,
        'bench_avg_parking': bench_avg_parking,
        'bench_parking_count': len(bench_parking_zones),
        'hist_avg_parking': hist_avg_parking,
        'hist_parking_costs': hist_parking_costs,
        'parking_range_low': parking_range_low,
        'parking_range_high': parking_range_high,
        'parking_mid': parking_mid,
        'gpm_scenarios': gpm_scenarios,
    }


def query_zones(team_id='gyeonggi'):
    """운영 존 + 차량 대수 + 부름 가능 여부 (is_d2d_car_exportable)"""
    region_filter = _region1_sql(team_id, 'cz')
    sql = f"""
    SELECT cz.id AS zone_id, cz.zone_name, cz.name AS parking_name,
           cz.lat, cz.lng, cz.region1, cz.region2, cz.region3, cz.address,
           cz.is_d2d_car_exportable, cz.imaginary,
           COUNT(DISTINCT ci.id) AS car_count
    FROM `socar-data.tianjin_replica.carzone_info` cz
    LEFT JOIN `socar-data.tianjin_replica.car_info` ci
      ON cz.id = ci.zone_id AND ci.state = 5 AND ci.level = 1
    WHERE cz.state = 1 AND {region_filter}
      AND cz.imaginary IN (0,3,5) AND cz.visibility = 1
    GROUP BY 1,2,3,4,5,6,7,8,9,10,11
    HAVING NOT (cz.imaginary = 0 AND COUNT(DISTINCT ci.id) = 0)
    ORDER BY cz.region2, cz.zone_name
    """
    return run_bq(sql)


def query_socar_supply_by_region(team_id='gyeonggi'):
    """쏘카 지역별 실 운영 차량/존 수 (profit_socar_car_daily 기준, 최근 3개월)"""
    region_filter = _region1_sql(team_id)
    sql = f"""
    SELECT
        region2,
        COUNT(DISTINCT zone_id) AS socar_zones,
        COUNT(DISTINCT car_id) AS socar_cars
    FROM `socar-data.socar_biz_profit.profit_socar_car_daily`
    WHERE {region_filter}
      AND car_state IN ('운영', '수리')
      AND car_sharing_type IN ('socar', 'zplus')
      AND date >= '{THREE_MONTHS_AGO}' AND date <= '{TODAY}'
    GROUP BY region2
    ORDER BY region2
    """
    rows = run_bq(sql)
    result = {}
    for r in rows:
        r2 = r.get('region2', '')
        if not r2:
            continue
        result[r2] = {
            'socar_zones': int(r.get('socar_zones', 0)),
            'socar_cars': int(r.get('socar_cars', 0)),
        }
    return result


def query_reservation_timeline(team_id='gyeonggi'):
    """존별 차량 예약 타임라인 (±1주), state != 0 (취소 제외)"""
    region_filter = _region1_sql(team_id, 'cz')
    one_week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    one_week_later = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    sql = f"""
    SELECT
        ci.zone_id,
        r.car_id,
        ci.car_name,
        ci.car_num,
        FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', r.start_at, 'Asia/Seoul') AS start_at,
        FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', r.end_at, 'Asia/Seoul') AS end_at,
        r.way,
        r.member_imaginary
    FROM `socar-data.tianjin_replica.reservation_info` r
    JOIN `socar-data.tianjin_replica.car_info` ci ON r.car_id = ci.id
    JOIN `socar-data.tianjin_replica.carzone_info` cz ON ci.zone_id = cz.id
    WHERE {region_filter}
      AND cz.state = 1 AND cz.imaginary IN (0,3,5) AND cz.visibility = 1
      AND r.state != 0
      AND r.end_at >= TIMESTAMP('{one_week_ago}', 'Asia/Seoul')
      AND r.start_at <= TIMESTAMP('{one_week_later}', 'Asia/Seoul')
    ORDER BY ci.zone_id, r.car_id, r.start_at
    """
    cmd = ["bq", "query", "--use_legacy_sql=false", "--format=json", "--max_rows=200000", sql]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        print(f"  [경고] 예약 타임라인 조회 실패: {result.stderr[:200]}")
        return {}
    rows = json.loads(result.stdout)
    # zone_id별로 그룹핑
    by_zone = {}
    for r in rows:
        zid = str(r['zone_id'])
        if zid not in by_zone:
            by_zone[zid] = []
        mi = int(r.get('member_imaginary', 0) or 0)
        by_zone[zid].append({
            'car_id': int(r['car_id']),
            'car_name': r.get('car_name', ''),
            'car_num': r.get('car_num', ''),
            'start': r['start_at'],
            'end': r['end_at'],
            'way': r.get('way', ''),
            'block': 1 if mi != 0 else 0,
        })
    return by_zone


def query_parking_contract(team_id='gyeonggi'):
    """존별 사업자명, 정산 방식, 대당 주차비"""
    region_filter = _region1_sql_col(team_id, 'z.region_1')
    sql = f"""
    SELECT
        z.legacy_zone_id AS zone_id,
        pr.name AS provider_name,
        CASE
            WHEN pp.settlement_type = 'PVSTP_BATCH' THEN '일괄정산'
            WHEN pp.settlement_type = 'PVSTP_INDIVIDUAL' THEN '개별정산'
            WHEN pp.settlement_type = 'PVSTP_RENT' THEN '임대'
            WHEN pp.settlement_type = 'PVSTP_FREE' THEN '무료'
            ELSE pp.settlement_type
        END AS settlement_type,
        CASE
            WHEN rp.payment_cycle = 'REPAC_MONTH' AND pp.settlement_type = 'PVSTP_RENT'
                THEN ROUND(SAFE_DIVIDE(rp.rent_price, IFNULL(COUNT(DISTINCT ci.id),1)),0)
            WHEN rp.payment_cycle = 'REPAC_QUARTER' AND pp.settlement_type = 'PVSTP_RENT'
                THEN ROUND(SAFE_DIVIDE(rp.rent_price, IFNULL(COUNT(DISTINCT ci.id),1))/3,0)
            WHEN rp.payment_cycle = 'REPAC_ONE_YEAR' AND pp.settlement_type = 'PVSTP_RENT'
                THEN ROUND(SAFE_DIVIDE(rp.rent_price, IFNULL(COUNT(DISTINCT ci.id),1))/12,0)
            WHEN rp.payment_cycle = 'REPAC_TWO_MONTHS' AND pp.settlement_type = 'PVSTP_RENT'
                THEN ROUND(SAFE_DIVIDE(rp.rent_price, IFNULL(COUNT(DISTINCT ci.id),1))/2,0)
            WHEN rp.payment_cycle = 'REPAC_HALF_YEAR' AND pp.settlement_type = 'PVSTP_RENT'
                THEN ROUND(SAFE_DIVIDE(rp.rent_price, IFNULL(COUNT(DISTINCT ci.id),1))/6,0)
            ELSE stp.price
        END AS price_per_car
    FROM `socar-data.socar_zone.zone` z
    LEFT JOIN `socar-data.socar_zone.parking` p ON p.id = z.parking_id
    LEFT JOIN `socar-data.socar_zone.parking_contract` c ON c.parking_id = p.id
    LEFT JOIN `socar-data.socar_zone.parking_settlement_policy` sp ON c.policy_id = sp.id
    LEFT JOIN `socar-data.socar_zone.parking_policy` pp ON sp.id = pp.policy_id
    LEFT JOIN `socar-data.socar_zone.settlement_type_policy` stp ON pp.id = stp.parking_policy_id
    LEFT JOIN `socar-data.socar_zone.rent_policy` rp ON pp.id = rp.parking_policy_id
    LEFT JOIN (
        SELECT ci.*, z2.parking_id
        FROM `socar-data.tianjin_replica.car_info` ci
        LEFT JOIN `socar-data.socar_zone.zone` z2 ON z2.legacy_zone_id = ci.zone_id
        WHERE ci.sharing_type = 'socar' AND ci.state IN (4,5) AND ci.level = 1
    ) ci ON ci.parking_id = z.parking_id
    LEFT JOIN `socar-data.socar_zone.parking_provider` pr ON c.provider_id = pr.id
    WHERE z.legacy_zone_id IS NOT NULL AND z.legacy_zone_id NOT IN (0)
        AND c.business_type <> 'CTRBT_CLEANING_BUSINESS'
        AND pp.settlement_type IS NOT NULL
        AND {region_filter}
    GROUP BY z.legacy_zone_id, pr.name, pp.settlement_type, rp.rent_price, stp.price, rp.payment_cycle
    """
    cmd = ["bq", "query", "--use_legacy_sql=false", "--format=json", "--max_rows=5000", sql]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(f"BQ error: {result.stderr}")
    rows = json.loads(result.stdout)
    result = {}
    for r in rows:
        zid = int(r.get('zone_id', 0))
        result[zid] = {
            'provider_name': r.get('provider_name', ''),
            'settlement_type': r.get('settlement_type', ''),
            'price_per_car': int(float(r.get('price_per_car', 0) or 0)),
        }
    return result


def query_gcar_zones(team_id='gyeonggi'):
    """그린카 존 현황 (gcar_zone_info_log 최근 스냅샷)"""
    region_filter = _region1_sql(team_id)
    sql = f"""
    WITH latest AS (
      SELECT MAX(date) AS d FROM `socar-data.greencar.gcar_zone_info_log`
    ),
    latest_hour AS (
      SELECT MAX(hour) AS h FROM `socar-data.greencar.gcar_zone_info_log`
      WHERE date = (SELECT d FROM latest)
    )
    SELECT zone_id, zone_name, region2, lat, lng, sig_name,
           COALESCE(total_car_cnt, available_car_cnt, 0) AS total_cars
    FROM `socar-data.greencar.gcar_zone_info_log`
    WHERE date = (SELECT d FROM latest)
      AND hour = (SELECT h FROM latest_hour)
      AND {region_filter}
      AND COALESCE(total_car_cnt, available_car_cnt, 0) > 0
    ORDER BY total_cars DESC
    """
    return run_bq(sql)


# ── 전기차 충전 로밍 사업자 매칭 키워드 ──
# 에버온: 미니EV 제외 전 차량 이용 가능
EVERON_KEYWORDS = [
    '에버온', '환경부', '기후에너지', '한국전력', 'GS차지비', '차지비', 'LG유플러스', '볼트업', '플러그인',
    'SK일렉링크', 'TBU', '딜라이브', '레드이엔지', '매니지온', '스타코프', '신세계아이앤씨', '스파로스',
    '씨어스', '이브이시스', '이지차저', '이카플러그', '제주에너지', '제주전기자동차', '차지인',
    '채비', '클린일렉스', '타디스', '파워큐브', '파킹클라우드', '플러그링크',
    '한국전기차충전', '한국전자금융', 'NICE인프라', '나이스차저', '현대엔지니어링',
    '현대자동차', 'E-pit', '휴맥스', 'HUMAX', '한국전기차인프라', '이브이루씨', '서울씨엔지', '에바',
]
# 차지비: 미니EV 전용
CHARGEV_KEYWORDS = [
    'GS차지비', '차지비', '제주에너지', '환경부', '기후에너지', 'GS칼텍스', '대한송유관',
    '매니지온', '씨어스', '타디스', '서울씨엔지', '한국전력', '레드이엔지', '이지차저',
    '클린일렉스', '아이파킹', '이엘일렉트릭', '한국전기차인프라', '스타코프', '신세계아이앤씨',
    '에버온', '파워큐브', '플러그링크', '한국전자금융', 'NICE인프라', '한화솔루션',
    'LG유플러스', '볼트업', '플러그인', 'SK일렉링크', '이브이시스', '채비',
    '한국전기차충전', '현대엔지니어링', '제주전기자동차', '휴맥스', 'HUMAX',
]


def _match_roaming(busi_nm, keywords):
    """사업자명이 로밍 키워드에 매칭되는지 확인"""
    for kw in keywords:
        if kw in busi_nm:
            return True
    return False


EV_CHARGER_API_KEY = '5145469085c4a37aebedd3f2859b7c4616180a6b57ce5a999e2afa96ed472e9a'
EV_CHARGER_ZCODES = {
    'seoul': ['11'],          # 서울
    'gyeonggi': ['41', '51'], # 경기, 강원(특별자치도)
    'chungcheong': ['43', '44', '30', '36'],  # 충북, 충남, 대전, 세종
    'gyeongbuk': ['47', '27'],  # 경북, 대구
    'gyeongnam': ['48', '26', '31'],  # 경남, 부산, 울산
    'honam': ['45', '46', '29'],  # 전북, 전남, 광주
}


def query_ev_chargers(team_id='gyeonggi'):
    """환경부 API로 전기차 충전소 정보 조회 → 충전소(statId)별 집계"""
    zcodes = EV_CHARGER_ZCODES.get(team_id, ['41'])
    all_chargers = []
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    for zcode in zcodes:
        page = 1
        while True:
            url = (f"http://apis.data.go.kr/B552584/EvCharger/getChargerInfo"
                   f"?serviceKey={EV_CHARGER_API_KEY}&numOfRows=9999&pageNo={page}"
                   f"&dataType=JSON&zcode={zcode}")
            req = urllib.request.Request(url, headers={'User-Agent': 'socar-demand-map/1.0'})
            try:
                with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                    data = json.loads(resp.read())
                items_wrapper = data.get('items', {})
                if isinstance(items_wrapper, dict):
                    items = items_wrapper.get('item', [])
                elif isinstance(items_wrapper, list):
                    items = items_wrapper
                else:
                    items = []
                if not items:
                    break
                all_chargers.extend(items)
                total = int(data.get('totalCount', 0))
                if page * 9999 >= total:
                    break
                page += 1
            except Exception as e:
                print(f"  [경고] 충전소 API 조회 실패 (zcode={zcode}, page={page}): {e}")
                break
            time.sleep(0.3)

    # 충전소(statId)별 집계: 위치, 이름, 급속/완속 충전기 수
    stations = {}
    for c in all_chargers:
        sid = c.get('statId', '')
        if not sid:
            continue
        lat = float(c.get('lat', 0) or 0)
        lng = float(c.get('lng', 0) or 0)
        if lat == 0 or lng == 0:
            continue
        chger_type = int(c.get('chgerType', 0) or 0)
        # chgerType: 01=DC차데모, 02=AC완속, 03=DC차데모+AC3상, 04=DC콤보, 05=DC차데모+DC콤보, 06=DC차데모+AC3상+DC콤보, 07=AC3상
        is_fast = chger_type in [1, 3, 4, 5, 6]

        if sid not in stations:
            stations[sid] = {
                'statId': sid,
                'statNm': c.get('statNm', ''),
                'addr': c.get('addr', ''),
                'lat': lat, 'lng': lng,
                'busiNm': c.get('busiNm', ''),
                'useTime': c.get('useTime', ''),
                'fast': 0, 'slow': 0,
            }
        if is_fast:
            stations[sid]['fast'] += 1
        else:
            stations[sid]['slow'] += 1

    result = list(stations.values())
    for s in result:
        s['total'] = s['fast'] + s['slow']
        s['everon'] = _match_roaming(s['busiNm'], EVERON_KEYWORDS)
        s['chargev'] = _match_roaming(s['busiNm'], CHARGEV_KEYWORDS)
    # 팀 관할 polygon 필터 (좌표 기준)
    team_poly = _get_team_polygon(team_id)
    if team_poly:
        before = len(result)
        result = [s for s in result if team_poly.contains(Point(s['lng'], s['lat']))]
        print(f"  충전소 API: {len(all_chargers)}건 → {before}개 → polygon 필터 후 {len(result)}개 충전소")
    else:
        print(f"  충전소 API: {len(all_chargers)}건 → {len(result)}개 충전소 집계")
    return result


def match_ev_to_zones(ev_data, zones_data, radius_km=0.1):
    """운영 존과 충전소 매칭 (100m 이내 = 같은 현장)"""
    for z in zones_data:
        zlat, zlng = float(z['lat']), float(z['lng'])
        nearby = [e for e in ev_data if haversine_km(zlat, zlng, e['lat'], e['lng']) <= radius_km]
        z['ev_stations'] = len(nearby)
        z['ev_fast'] = sum(e['fast'] for e in nearby)
        z['ev_slow'] = sum(e['slow'] for e in nearby)
        z['ev_total'] = sum(e['total'] for e in nearby)
        # 충전소별 상세 (이름 + 기수 + 동일현장 판별)
        zname = z.get('zone_name', '').replace(' ', '')
        pname = z.get('parking_name', '').replace(' ', '')
        z['ev_detail'] = []
        for e in nearby:
            ename = e['statNm'].replace(' ', '')
            # 존/주차장 이름이 충전소 이름에 포함되거나 그 반대면 동일 현장
            is_same = False
            for kw in [zname, pname]:
                if len(kw) >= 2 and (kw in ename or ename in kw):
                    is_same = True
                    break
                # 3글자 이상 부분 매칭
                for i in range(len(kw) - 2):
                    sub = kw[i:i+3]
                    if sub in ename:
                        is_same = True
                        break
                if is_same:
                    break
            z['ev_detail'].append({
                'name': ename, 'fast': e['fast'], 'slow': e['slow'], 'total': e['total'],
                'dist': round(haversine_km(zlat, zlng, e['lat'], e['lng']) * 1000),
                'same': is_same,
                'congestion': e.get('congestion')
            })
        z['ev_detail'].sort(key=lambda x: (not x['same'], x['dist']))
        # 충전보장 EV 존인데 이름 매칭된 현장 충전기가 없으면, 가장 가까운 충전소를 현장으로 간주
        has_ev_zone = z.get('ev_zone') is not None
        if has_ev_zone and not any(e['same'] for e in z['ev_detail']) and z['ev_detail']:
            z['ev_detail'][0]['same'] = True
        z['ev_same_total'] = sum(e['total'] for e in z['ev_detail'] if e['same'])
        z['ev_same_fast'] = sum(e['fast'] for e in z['ev_detail'] if e['same'])
        z['ev_same_slow'] = sum(e['slow'] for e in z['ev_detail'] if e['same'])


def compute_ev_congestion():
    """충전소별 시간대별 혼잡도 집계 (수집 로그 기반)"""
    from collections import defaultdict
    log_dir = os.path.join(OUTPUT_DIR, '.cache', 'ev_status_log')
    if not os.path.exists(log_dir):
        return {}

    # 충전소별 시간대별 충전중/전체 집계
    station_hourly = defaultdict(lambda: defaultdict(lambda: {'charging': 0, 'total': 0}))
    file_count = 0
    for fname in sorted(os.listdir(log_dir)):
        if not fname.endswith('.jsonl'):
            continue
        for line in open(os.path.join(log_dir, fname)):
            try:
                d = json.loads(line)
            except Exception:
                continue
            hour = d['hour']
            for sid, s in d.get('stations', {}).items():
                station_hourly[sid][hour]['charging'] += s['charging']
                station_hourly[sid][hour]['total'] += s['total']
        file_count += 1

    if file_count == 0:
        return {}

    # 충전소별 24시간 혼잡도 배열 (0~23시, 비율 0~100)
    result = {}
    for sid, hours in station_hourly.items():
        hourly = []
        for h in range(24):
            if h in hours and hours[h]['total'] > 0:
                rate = round(hours[h]['charging'] / hours[h]['total'] * 100)
            else:
                rate = -1  # 데이터 없음
            hourly.append(rate)
        result[sid] = hourly

    print(f"  충전 혼잡도: {len(result)}개 충전소, {file_count}일 데이터")
    return result


def query_ev_car_zones(team_id='gyeonggi'):
    """EV 차량이 배치된 존 조회 → zone_id: {ev_car_count, ev_charged_count}"""
    region_filter = _region1_sql_col(team_id, 'cz.region1')
    sql = f"""
    SELECT ci.zone_id,
           COUNT(DISTINCT ci.id) AS ev_car_count,
           COUNTIF(ci.car_name LIKE '%충전보장%') AS ev_charged_count
    FROM `socar-data.tianjin_replica.car_info` ci
    JOIN `socar-data.tianjin_replica.carzone_info` cz ON ci.zone_id = cz.id
    WHERE (ci.car_name LIKE '%EV%' OR ci.car_name LIKE '%아이오닉%' OR ci.car_name LIKE '%니로 EV%')
      AND ci.state IN (4,5) AND ci.level = 1
      AND ci.sharing_type IN ('socar','zplus')
      AND cz.state = 1 AND {region_filter}
    GROUP BY ci.zone_id
    """
    cmd = ["bq", "query", "--use_legacy_sql=false", "--format=json", "--max_rows=500", sql]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        print(f"  [경고] EV 차량 존 조회 실패: {result.stderr[:200]}")
        return {}
    rows = json.loads(result.stdout)
    ev_map = {}
    for r in rows:
        zid = int(r['zone_id'])
        ev_map[zid] = {
            'ev_car_count': int(r['ev_car_count']),
            'ev_charged_count': int(r['ev_charged_count']),
        }
    print(f"  EV 차량 존: {len(ev_map)}개 ({sum(v['ev_car_count'] for v in ev_map.values())}대)")
    return ev_map


def query_ev_virtual_zones(team_id='gyeonggi'):
    """충전보장형 전기차 가상존 조회 — original_zone_id로 원본존에 매핑"""
    region_filter = _region1_sql_col(team_id, 'z.region_1')
    sql = f"""
    WITH ev_zones AS (
      SELECT z.legacy_zone_id AS zone_id, cz.zone_name, z.parking_id, cz.imaginary,
             cz.is_d2d_car_exportable,
             (SELECT COUNT(DISTINCT ci.id) FROM `socar-data.tianjin_replica.car_info` ci
              WHERE ci.zone_id = z.legacy_zone_id AND ci.state IN (4,5) AND ci.level = 1) AS car_count
      FROM `socar-data.socar_zone.zone` z
      JOIN `socar-data.tianjin_replica.carzone_info` cz ON cz.id = z.legacy_zone_id
      WHERE z.incidental = 1 AND cz.zone_name LIKE '%전기차%' AND cz.state = 1
        AND {region_filter}
    ),
    original AS (
      SELECT z.legacy_zone_id AS zone_id, z.parking_id
      FROM `socar-data.socar_zone.zone` z
      JOIN `socar-data.tianjin_replica.carzone_info` cz ON cz.id = z.legacy_zone_id
      WHERE z.incidental = 0 AND cz.state = 1
    ),
    profit AS (
      SELECT zone_id,
             SUM(revenue) AS total_revenue,
             ROUND(SAFE_DIVIDE(SUM(revenue)*28, NULLIF(SUM(opr_day),0))) AS revenue_per_car_28d,
             ROUND(SAFE_DIVIDE(SUM(profit)*28, NULLIF(SUM(opr_day),0))) AS gp_per_car_28d
      FROM `socar-data.socar_biz_profit.profit_socar_car_daily`
      WHERE date >= DATE_SUB(CURRENT_DATE(), INTERVAL 28 DAY)
        AND car_sharing_type IN ('socar','zplus')
      GROUP BY zone_id
    ),
    util AS (
      SELECT zone_id,
             ROUND(SAFE_DIVIDE(SUM(op_min), SUM(dp_min) - SUM(bl_min)) * 100, 1) AS utilization_rate
      FROM `socar-data.socar_biz.operation_per_car_daily_v2`
      WHERE date >= DATE_SUB(CURRENT_DATE(), INTERVAL 28 DAY)
      GROUP BY zone_id
    )
    SELECT ev.zone_id, ev.zone_name, ev.car_count, ev.imaginary, ev.is_d2d_car_exportable,
           o.zone_id AS original_zone_id,
           COALESCE(p.total_revenue, 0) AS total_revenue,
           COALESCE(p.revenue_per_car_28d, 0) AS revenue_per_car_28d,
           COALESCE(p.gp_per_car_28d, 0) AS gp_per_car_28d,
           COALESCE(u.utilization_rate, 0) AS utilization_rate
    FROM ev_zones ev
    JOIN original o ON o.parking_id = ev.parking_id
    LEFT JOIN profit p ON p.zone_id = ev.zone_id
    LEFT JOIN util u ON u.zone_id = ev.zone_id
    """
    cmd = ["bq", "query", "--use_legacy_sql=false", "--format=json", "--max_rows=500", sql]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        print(f"  [경고] EV 가상존 조회 실패: {result.stderr[:300]}")
        return []
    rows = json.loads(result.stdout)
    # original_zone_id → ev_zone 매핑
    ev_map = {}
    for r in rows:
        oid = int(r['original_zone_id'])
        ev_map[oid] = {
            'zone_id': int(r['zone_id']),
            'zone_name': r['zone_name'],
            'car_count': int(r['car_count']),
            'imaginary': int(r.get('imaginary', 0)),
            'is_d2d': r.get('is_d2d_car_exportable', '') == 'ABLE',
            'total_revenue': int(float(r.get('total_revenue', 0))),
            'revenue_per_car_28d': int(float(r.get('revenue_per_car_28d', 0))),
            'gp_per_car_28d': int(float(r.get('gp_per_car_28d', 0))),
            'utilization_rate': float(r.get('utilization_rate', 0)),
        }
    print(f"  EV 가상존: {len(ev_map)}개 (원본존 매핑)")
    return ev_map


def query_closed_zones(team_id='gyeonggi'):
    """폐쇄 존 현황: 과거 운영 이력이 있으나 현재 미운영인 모든 존"""
    z_region_filter = _region1_sql_col(team_id, 'z.region_1')
    region_filter = _region1_sql(team_id)
    sql = f"""
    WITH all_zones AS (
      SELECT z.legacy_zone_id AS zone_id, cz.zone_name, cz.name AS parking_name,
             cz.lat, cz.lng, cz.region2, cz.region3, cz.address,
             cz.state, cz.imaginary
      FROM `socar-data.socar_zone.zone` z
      JOIN `socar-data.tianjin_replica.carzone_info` cz ON cz.id = z.legacy_zone_id
      WHERE {z_region_filter}
        AND z.type IN ('ZONTP_NORMAL','ZONTP_D2D_STATION','ZONTP_D2D_PRIORITY')
        AND NOT REGEXP_CONTAINS(cz.zone_name, r'(매각|비즈니스|시승|장착|캐리어|캠핑카|경매|부름호출존|플랜)')
    ),
    active_zones AS (
      SELECT zone_id
      FROM `socar-data.socar_biz_profit.profit_socar_car_daily`
      WHERE date = CURRENT_DATE() - 1
        AND opr_day > 0
      GROUP BY zone_id
    ),
    closed AS (
      SELECT az.*
      FROM all_zones az
      LEFT JOIN active_zones act ON az.zone_id = act.zone_id
      WHERE act.zone_id IS NULL
    ),
    hist AS (
      SELECT zone_id,
             MIN(date) AS first_date,
             MAX(date) AS last_date,
             COUNT(DISTINCT date) AS operation_days,
             ROUND(SAFE_DIVIDE(SUM(opr_day), COUNT(DISTINCT date)), 1) AS hist_car_count,
             ROUND(SAFE_DIVIDE(SUM(revenue)*28, NULLIF(SUM(opr_day),0))) AS revenue_per_car_28d,
             ROUND(SAFE_DIVIDE(SUM(profit)*28, NULLIF(SUM(opr_day),0))) AS gp_per_car_28d
      FROM `socar-data.socar_biz_profit.profit_socar_car_daily`
      WHERE {region_filter}
        AND car_state IN ('운영', '수리')
        AND car_sharing_type IN ('socar', 'zplus')
      GROUP BY zone_id
    ),
    util AS (
      SELECT zone_id,
             ROUND(SAFE_DIVIDE(SUM(op_min), SUM(dp_min) - SUM(bl_min)) * 100, 1) AS utilization_rate
      FROM `socar-data.socar_biz.operation_per_car_daily_v2`
      WHERE {region_filter}
      GROUP BY zone_id
    )
    SELECT c.*,
           CAST(h.first_date AS STRING) AS first_date,
           CAST(h.last_date AS STRING) AS last_date,
           COALESCE(h.operation_days, 0) AS operation_days,
           COALESCE(h.hist_car_count, 0) AS hist_car_count,
           COALESCE(h.revenue_per_car_28d, 0) AS revenue_per_car_28d,
           COALESCE(h.gp_per_car_28d, 0) AS gp_per_car_28d,
           COALESCE(u.utilization_rate, 0) AS utilization_rate
    FROM closed c
    INNER JOIN hist h ON c.zone_id = h.zone_id
    LEFT JOIN util u ON c.zone_id = u.zone_id
    ORDER BY c.region2, c.zone_name
    """
    cmd = ["bq", "query", "--use_legacy_sql=false", "--format=json", "--max_rows=5000", sql]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        print(f"  [경고] 폐쇄 존 조회 실패: {result.stderr[:300] or result.stdout[:300]}")
        return []
    out = result.stdout.strip()
    if out.startswith('Error') or out.startswith('Syntax'):
        print(f"  [경고] 폐쇄 존 조회 실패: {out[:300]}")
        return []
    return json.loads(out)


def query_zone_profit(team_id='gyeonggi'):
    """존별 실적 (profit_socar_car_daily + operation_per_car_daily_v2, 최근 3개월)
    대당 매출/GP = SUM(revenue or profit) * 28 / SUM(opr_day) → 28일 기준"""
    region_filter = _region1_sql(team_id)
    sql = f"""
    WITH profit AS (
        SELECT
            zone_id,
            ROUND(SAFE_DIVIDE(SUM(revenue) * 28, NULLIF(COUNT(DISTINCT date), 0))) AS total_revenue,
            ROUND(SAFE_DIVIDE(SUM(revenue) * 28, NULLIF(SUM(opr_day), 0))) AS revenue_per_car_28d,
            ROUND(SAFE_DIVIDE(SUM(profit) * 28, NULLIF(SUM(opr_day), 0))) AS gp_per_car_28d,
            ROUND(SAFE_DIVIDE(SUM(revenue), NULLIF(SUM(nuse), 0))) AS revenue_per_res
        FROM `socar-data.socar_biz_profit.profit_socar_car_daily`
        WHERE {region_filter}
            AND car_state IN ('운영', '수리')
            AND car_sharing_type IN ('socar', 'zplus')
            AND date >= '{THREE_MONTHS_AGO}' AND date <= '{TODAY}'
        GROUP BY zone_id
    ),
    util AS (
        SELECT
            zone_id,
            ROUND(SAFE_DIVIDE(SUM(op_min), SUM(dp_min) - SUM(bl_min)) * 100, 1) AS utilization_rate
        FROM `socar-data.socar_biz.operation_per_car_daily_v2`
        WHERE {region_filter}
            AND date >= '{THREE_MONTHS_AGO}' AND date <= '{TODAY}'
        GROUP BY zone_id
    )
    SELECT p.*, COALESCE(u.utilization_rate, 0) AS utilization_rate
    FROM profit p
    LEFT JOIN util u ON p.zone_id = u.zone_id
    ORDER BY p.total_revenue DESC
    """
    cmd = ["bq", "query", "--use_legacy_sql=false", "--format=json", "--max_rows=2000", sql]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        print(f"  [경고] 존 실적 조회 실패: {result.stderr[:200]}")
        return {}
    rows = json.loads(result.stdout)
    profit_by_zone = {}
    for r in rows:
        zid = int(r['zone_id'])
        profit_by_zone[zid] = {
            'total_revenue': int(float(r.get('total_revenue', 0) or 0)),
            'revenue_per_car_28d': int(float(r.get('revenue_per_car_28d', 0) or 0)),
            'gp_per_car_28d': int(float(r.get('gp_per_car_28d', 0) or 0)),
            'utilization_rate': float(r.get('utilization_rate', 0) or 0),
            'revenue_per_res': int(float(r.get('revenue_per_res', 0) or 0)),
        }
    return profit_by_zone


def query_dashboard_metrics(team_id='gyeonggi'):
    """ISOYEAR/ISOWEEK 기준 주간 실적 집계 (2025~2026) — 레퍼런스 쿼리 기반
    존 제외 필터 적용 (매각/비즈니스/시승 등), car_count=SUM(opr_day)/7, 미완성 주 제외
    """
    z_region = _region1_sql_col(team_id, 'z.region_1')
    excl = r'(매각|비즈니스|시승|장착|캐리어|캠핑카|경매|부름호출존|플랜)'
    sql = f"""
    WITH
    demand AS (
      SELECT
        EXTRACT(ISOYEAR FROM DATE(l.start_at, 'Asia/Seoul')) AS isoyear,
        EXTRACT(ISOWEEK  FROM DATE(l.start_at, 'Asia/Seoul')) AS isoweek,
        COUNT(*) AS attempt,
        SAFE_DIVIDE(COUNTIF(l.available_car_count = 0), COUNT(*)) AS fail_rate
      FROM `socar-data.service_metrics.log_get_car_classes` l
      INNER JOIN `socar-data.socar_zone.zone` z ON l.zone_id = z.legacy_zone_id
      WHERE {z_region}
        AND NOT REGEXP_CONTAINS(z.name, r'{excl}')
        AND EXTRACT(ISOYEAR FROM DATE(l.start_at, 'Asia/Seoul')) IN (2025, 2026)
        AND DATE(l.start_at, 'Asia/Seoul') < CURRENT_DATE('Asia/Seoul')
        AND NOT (
          EXTRACT(ISOYEAR FROM DATE(l.start_at, 'Asia/Seoul')) = EXTRACT(ISOYEAR FROM CURRENT_DATE('Asia/Seoul'))
          AND EXTRACT(ISOWEEK  FROM DATE(l.start_at, 'Asia/Seoul')) >= EXTRACT(ISOWEEK FROM CURRENT_DATE('Asia/Seoul'))
        )
      GROUP BY 1, 2
    ),
    profit AS (
      SELECT
        EXTRACT(ISOYEAR FROM p.date) AS isoyear,
        EXTRACT(ISOWEEK  FROM p.date) AS isoweek,
        SAFE_DIVIDE(SUM(p.opr_day), 7)                               AS car_count,
        COUNT(DISTINCT p.zone_id)                                    AS zone_count,
        SUM(p.revenue)                                               AS revenue,
        SUM(p.profit)                                                AS profit,
        SUM(p.nuse)                                                  AS use_count,
        SAFE_DIVIDE(SUM(p.profit),  SUM(p.revenue))                  AS gpm,
        SAFE_DIVIDE(SUM(p.revenue), SAFE_DIVIDE(SUM(p.opr_day), 7))  AS rev_per_car
      FROM `socar-data.socar_biz_profit.profit_socar_car_daily` p
      INNER JOIN `socar-data.socar_zone.zone` z ON z.legacy_zone_id = p.zone_id
      WHERE {z_region}
        AND p.car_sharing_type IN ('socar', 'zplus')
        AND p.car_state IN ('운영', '수리')
        AND NOT REGEXP_CONTAINS(z.name, r'{excl}')
        AND EXTRACT(ISOYEAR FROM p.date) IN (2025, 2026)
        AND p.date < CURRENT_DATE('Asia/Seoul')
        AND NOT (
          EXTRACT(ISOYEAR FROM p.date) = EXTRACT(ISOYEAR FROM CURRENT_DATE('Asia/Seoul'))
          AND EXTRACT(ISOWEEK  FROM p.date) >= EXTRACT(ISOWEEK FROM CURRENT_DATE('Asia/Seoul'))
        )
      GROUP BY 1, 2
    ),
    way AS (
      SELECT
        EXTRACT(ISOYEAR FROM r.return_at_kst) AS isoyear,
        EXTRACT(ISOWEEK  FROM r.return_at_kst) AS isoweek,
        CASE
          WHEN r.way = 'round'                     THEN '왕복'
          WHEN r.way IN ('d2d_oneway','d2d_round') THEN '부름'
          WHEN r.way = 'z2d_oneway'               THEN '편도'
          ELSE '기타'
        END AS way_type,
        COUNT(DISTINCT r.reservation_id)          AS w_count,
        SUM(r.utime)                              AS w_utime,
        SUM(r.revenue)                            AS w_revenue,
        SUM(r.__rev_rent)                         AS w_price,
        SUM(r.__rev_rent_discount)                AS w_discount,
        COUNTIF(r.utime < 240)                      AS w_u_0to4h,
        COUNTIF(r.utime >= 240 AND r.utime < 480)   AS w_u_4to8h,
        COUNTIF(r.utime >= 480 AND r.utime < 1440)  AS w_u_8to24h,
        COUNTIF(r.utime >= 1440)                    AS w_u_24plus,
        SUM(IF(r.utime < 240,                    r.revenue, 0)) AS w_u_0to4h_rev,
        SUM(IF(r.utime >= 240 AND r.utime < 480, r.revenue, 0)) AS w_u_4to8h_rev,
        SUM(IF(r.utime >= 480 AND r.utime < 1440,r.revenue, 0)) AS w_u_8to24h_rev,
        SUM(IF(r.utime >= 1440,                  r.revenue, 0)) AS w_u_24plus_rev
      FROM `socar-data.socar_biz_profit.profit_socar_reservation` r
      INNER JOIN `socar-data.socar_zone.zone` z ON r.zone_id = z.legacy_zone_id
      WHERE {z_region}
        AND NOT REGEXP_CONTAINS(z.name, r'{excl}')
        AND EXTRACT(ISOYEAR FROM r.return_at_kst) IN (2025, 2026)
        AND r.return_at_kst < CAST(CURRENT_DATE('Asia/Seoul') AS DATETIME)
        AND NOT (
          EXTRACT(ISOYEAR FROM r.return_at_kst) = EXTRACT(ISOYEAR FROM CURRENT_DATE('Asia/Seoul'))
          AND EXTRACT(ISOWEEK  FROM r.return_at_kst) >= EXTRACT(ISOWEEK FROM CURRENT_DATE('Asia/Seoul'))
        )
      GROUP BY 1, 2, 3
    ),
    way_pivot AS (
      SELECT
        isoyear, isoweek,
        SAFE_DIVIDE(-SUM(w_discount), NULLIF(SUM(w_price), 0))       AS discount_rate,
        SUM(IF(way_type='왕복', w_count,   0))                       AS round_count,
        SAFE_DIVIDE(SUM(IF(way_type='왕복', w_revenue, 0)),
                    NULLIF(SUM(IF(way_type='왕복', w_count, 0)),0))   AS round_rev_per_use,
        SAFE_DIVIDE(SUM(IF(way_type='왕복', w_utime, 0)),
                    NULLIF(SUM(IF(way_type='왕복', w_count, 0)),0)) / 60.0 AS round_avg_hours,
        SUM(IF(way_type='편도', w_count,   0))                       AS oneway_count,
        SAFE_DIVIDE(SUM(IF(way_type='편도', w_revenue, 0)),
                    NULLIF(SUM(IF(way_type='편도', w_count, 0)),0))   AS oneway_rev_per_use,
        SAFE_DIVIDE(SUM(IF(way_type='편도', w_utime, 0)),
                    NULLIF(SUM(IF(way_type='편도', w_count, 0)),0)) / 60.0 AS oneway_avg_hours,
        SUM(IF(way_type='부름', w_count,   0))                       AS d2d_count,
        SAFE_DIVIDE(SUM(IF(way_type='부름', w_revenue, 0)),
                    NULLIF(SUM(IF(way_type='부름', w_count, 0)),0))   AS d2d_rev_per_use,
        SAFE_DIVIDE(SUM(IF(way_type='부름', w_utime, 0)),
                    NULLIF(SUM(IF(way_type='부름', w_count, 0)),0)) / 60.0 AS d2d_avg_hours,
        -- utime 구간별 건수·매출 (전체 way_type 합산)
        SUM(w_u_0to4h)      AS u_0to4h,
        SUM(w_u_4to8h)      AS u_4to8h,
        SUM(w_u_8to24h)     AS u_8to24h,
        SUM(w_u_24plus)     AS u_24plus,
        SUM(w_u_0to4h_rev)  AS u_0to4h_rev,
        SUM(w_u_4to8h_rev)  AS u_4to8h_rev,
        SUM(w_u_8to24h_rev) AS u_8to24h_rev,
        SUM(w_u_24plus_rev) AS u_24plus_rev
      FROM way GROUP BY 1, 2
    ),
    payment AS (
      SELECT
        EXTRACT(ISOYEAR FROM DATE(v.timeMs, 'Asia/Seoul')) AS isoyear,
        EXTRACT(ISOWEEK  FROM DATE(v.timeMs, 'Asia/Seoul')) AS isoweek,
        COUNT(*) AS payment_attempt
      FROM `socar-data.socar_server_3.GET_CURRENT_CAR_RENTAL_VIEW` v
      INNER JOIN `socar-data.socar_zone.zone` z
             ON SAFE_CAST(v.carRental.startPoint.zoneId AS INT64) = z.legacy_zone_id
      WHERE {z_region}
        AND v.carRental.startPoint.zoneId IS NOT NULL
        AND EXTRACT(ISOYEAR FROM DATE(v.timeMs, 'Asia/Seoul')) IN (2025, 2026)
        AND DATE(v.timeMs, 'Asia/Seoul') < CURRENT_DATE('Asia/Seoul')
        AND NOT (
          EXTRACT(ISOYEAR FROM DATE(v.timeMs, 'Asia/Seoul')) = EXTRACT(ISOYEAR FROM CURRENT_DATE('Asia/Seoul'))
          AND EXTRACT(ISOWEEK  FROM DATE(v.timeMs, 'Asia/Seoul')) >= EXTRACT(ISOWEEK FROM CURRENT_DATE('Asia/Seoul'))
        )
      GROUP BY 1, 2
    ),
    util AS (
      SELECT
        EXTRACT(ISOYEAR FROM o.date) AS isoyear,
        EXTRACT(ISOWEEK  FROM o.date) AS isoweek,
        SAFE_DIVIDE(
          SUM(IF(EXTRACT(DAYOFWEEK FROM o.date) BETWEEN 2 AND 6, o.op_min, 0)),
          NULLIF(SUM(IF(EXTRACT(DAYOFWEEK FROM o.date) BETWEEN 2 AND 6,
                       o.dp_min - o.bl_min, 0)), 0)
        ) AS opr_weekday,
        SAFE_DIVIDE(
          SUM(IF(EXTRACT(DAYOFWEEK FROM o.date) IN (1, 7), o.op_min, 0)),
          NULLIF(SUM(IF(EXTRACT(DAYOFWEEK FROM o.date) IN (1, 7),
                       o.dp_min - o.bl_min, 0)), 0)
        ) AS opr_weekend
      FROM `socar-data.socar_biz.operation_per_car_daily_v2` o
      INNER JOIN `socar-data.socar_zone.zone` z ON o.zone_id = z.legacy_zone_id
      WHERE {z_region}
        AND NOT REGEXP_CONTAINS(z.name, r'{excl}')
        AND EXTRACT(ISOYEAR FROM o.date) IN (2025, 2026)
        AND o.date < CURRENT_DATE('Asia/Seoul')
        AND NOT (
          EXTRACT(ISOYEAR FROM o.date) = EXTRACT(ISOYEAR FROM CURRENT_DATE('Asia/Seoul'))
          AND EXTRACT(ISOWEEK  FROM o.date) >= EXTRACT(ISOWEEK FROM CURRENT_DATE('Asia/Seoul'))
        )
      GROUP BY 1, 2
    )
    SELECT
      p.isoyear, p.isoweek,
      CASE WHEN p.isoyear = EXTRACT(ISOYEAR FROM CURRENT_DATE('Asia/Seoul'))
           THEN 'now' ELSE 'yoy' END AS period_type,
      p.car_count, p.zone_count,
      d.attempt, d.fail_rate,
      p.revenue, p.profit, p.gpm, p.rev_per_car,
      p.use_count,
      wp.discount_rate,
      wp.round_count,   wp.round_rev_per_use,  wp.round_avg_hours,
      wp.oneway_count,  wp.oneway_rev_per_use, wp.oneway_avg_hours,
      wp.d2d_count,     wp.d2d_rev_per_use,    wp.d2d_avg_hours,
      u.opr_weekday, u.opr_weekend,
      IFNULL(wp.u_0to4h,      0) AS u_0to4h,
      IFNULL(wp.u_4to8h,      0) AS u_4to8h,
      IFNULL(wp.u_8to24h,     0) AS u_8to24h,
      IFNULL(wp.u_24plus,     0) AS u_24plus,
      IFNULL(wp.u_0to4h_rev,  0) AS u_0to4h_rev,
      IFNULL(wp.u_4to8h_rev,  0) AS u_4to8h_rev,
      IFNULL(wp.u_8to24h_rev, 0) AS u_8to24h_rev,
      IFNULL(wp.u_24plus_rev, 0) AS u_24plus_rev,
      IFNULL(pay.payment_attempt, 0) AS payment_attempt
    FROM profit p
    LEFT JOIN demand    d   ON p.isoyear = d.isoyear   AND p.isoweek = d.isoweek
    LEFT JOIN way_pivot wp  ON p.isoyear = wp.isoyear  AND p.isoweek = wp.isoweek
    LEFT JOIN util      u   ON p.isoyear = u.isoyear   AND p.isoweek = u.isoweek
    LEFT JOIN payment   pay ON p.isoyear = pay.isoyear AND p.isoweek = pay.isoweek
    ORDER BY p.isoyear DESC, p.isoweek DESC
    """
    try:
        rows = run_bq(sql, max_rows=200)
    except Exception as e:
        print(f"  [경고] 대시보드 주간 실적 조회 실패: {e}")
        return {'weekly': []}
    weekly = []
    for r in rows:
        weekly.append({
            'isoyear':           int(r['isoyear']),
            'isoweek':           int(r['isoweek']),
            'period_type':       r['period_type'],
            'car_count':         round(float(r.get('car_count') or 0), 1),
            'zone_count':        int(r.get('zone_count') or 0),
            'attempt':           int(r.get('attempt') or 0),
            'fail_rate':         round(float(r.get('fail_rate') or 0), 4),
            'revenue':           int(float(r.get('revenue') or 0)),
            'profit':            int(float(r.get('profit') or 0)),
            'gpm':               round(float(r.get('gpm') or 0), 4),
            'rev_per_car':       round(float(r.get('rev_per_car') or 0)),
            'use_count':         int(float(r.get('use_count') or 0)),
            'discount_rate':     round(float(r.get('discount_rate') or 0), 4),
            'round_count':       int(float(r.get('round_count') or 0)),
            'round_rev_per_use': round(float(r.get('round_rev_per_use') or 0)),
            'round_avg_hours':   round(float(r.get('round_avg_hours') or 0), 2),
            'oneway_count':      int(float(r.get('oneway_count') or 0)),
            'oneway_rev_per_use':round(float(r.get('oneway_rev_per_use') or 0)),
            'oneway_avg_hours':  round(float(r.get('oneway_avg_hours') or 0), 2),
            'd2d_count':         int(float(r.get('d2d_count') or 0)),
            'd2d_rev_per_use':   round(float(r.get('d2d_rev_per_use') or 0)),
            'd2d_avg_hours':     round(float(r.get('d2d_avg_hours') or 0), 2),
            'opr_weekday':       round(float(r.get('opr_weekday') or 0), 4),
            'opr_weekend':       round(float(r.get('opr_weekend') or 0), 4),
            'u_0to4h':           int(float(r.get('u_0to4h') or 0)),
            'u_4to8h':           int(float(r.get('u_4to8h') or 0)),
            'u_8to24h':          int(float(r.get('u_8to24h') or 0)),
            'u_24plus':          int(float(r.get('u_24plus') or 0)),
            'u_0to4h_rev':       int(float(r.get('u_0to4h_rev') or 0)),
            'u_4to8h_rev':       int(float(r.get('u_4to8h_rev') or 0)),
            'u_8to24h_rev':      int(float(r.get('u_8to24h_rev') or 0)),
            'u_24plus_rev':      int(float(r.get('u_24plus_rev') or 0)),
            'payment_attempt':   int(float(r.get('payment_attempt') or 0)),
        })
    return {'weekly': weekly, 'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M')}


def query_dashboard_by_region(team_id='gyeonggi', region_level='region2'):
    """지역별 주간 실적 집계 (region2 또는 region3 단위) — 전체 메트릭 포함
    carzone_info의 region2/region3 컬럼 기준, region1 포함 반환
    매출/GPM/이용건수/가동률/왕복·편도·부름/수요 모두 포함
    """
    z_region = _region1_sql_col(team_id, 'z.region_1')
    excl = r'(매각|비즈니스|시승|장착|캐리어|캠핑카|경매|부름호출존|플랜)'
    region_col = f'cz.{region_level}'
    parent_col = 'cz.region2' if region_level == 'region3' else "CAST('' AS STRING)"
    week_filter = """
        AND EXTRACT(ISOYEAR FROM p.date) IN (2025, 2026)
        AND p.date < CURRENT_DATE('Asia/Seoul')
        AND NOT (
          EXTRACT(ISOYEAR FROM p.date) = EXTRACT(ISOYEAR FROM CURRENT_DATE('Asia/Seoul'))
          AND EXTRACT(ISOWEEK FROM p.date) >= EXTRACT(ISOWEEK FROM CURRENT_DATE('Asia/Seoul'))
        )"""
    sql = f"""
    WITH
    perf AS (
      SELECT
        z.region_1 AS region1, {parent_col} AS region2_parent, {region_col} AS region,
        EXTRACT(ISOYEAR FROM p.date) AS isoyear, EXTRACT(ISOWEEK FROM p.date) AS isoweek,
        SAFE_DIVIDE(SUM(p.opr_day), 7)                              AS car_count,
        COUNT(DISTINCT p.zone_id)                                   AS zone_count,
        SUM(p.revenue)                                              AS revenue,
        SUM(p.profit)                                               AS profit,
        SUM(p.nuse)                                                 AS use_count,
        SAFE_DIVIDE(SUM(p.profit), SUM(p.revenue))                  AS gpm,
        SAFE_DIVIDE(SUM(p.revenue), SAFE_DIVIDE(SUM(p.opr_day),7))  AS rev_per_car
      FROM `socar-data.socar_biz_profit.profit_socar_car_daily` p
      INNER JOIN `socar-data.socar_zone.zone` z ON z.legacy_zone_id = p.zone_id
      INNER JOIN `socar-data.tianjin_replica.carzone_info` cz ON cz.id = z.legacy_zone_id
      WHERE {z_region}
        AND p.car_sharing_type IN ('socar', 'zplus')
        AND p.car_state IN ('운영', '수리')
        AND NOT REGEXP_CONTAINS(z.name, r'{excl}')
        {week_filter}
      GROUP BY 1, 2, 3, 4, 5
    ),
    util AS (
      SELECT
        z.region_1 AS region1, {parent_col} AS region2_parent, {region_col} AS region,
        EXTRACT(ISOYEAR FROM o.date) AS isoyear, EXTRACT(ISOWEEK FROM o.date) AS isoweek,
        SAFE_DIVIDE(
          SUM(IF(EXTRACT(DAYOFWEEK FROM o.date) BETWEEN 2 AND 6, o.op_min, 0)),
          NULLIF(SUM(IF(EXTRACT(DAYOFWEEK FROM o.date) BETWEEN 2 AND 6, o.dp_min - o.bl_min, 0)), 0)
        ) AS opr_weekday,
        SAFE_DIVIDE(
          SUM(IF(EXTRACT(DAYOFWEEK FROM o.date) IN (1,7), o.op_min, 0)),
          NULLIF(SUM(IF(EXTRACT(DAYOFWEEK FROM o.date) IN (1,7), o.dp_min - o.bl_min, 0)), 0)
        ) AS opr_weekend
      FROM `socar-data.socar_biz.operation_per_car_daily_v2` o
      INNER JOIN `socar-data.socar_zone.zone` z ON o.zone_id = z.legacy_zone_id
      INNER JOIN `socar-data.tianjin_replica.carzone_info` cz ON cz.id = z.legacy_zone_id
      WHERE {z_region}
        AND NOT REGEXP_CONTAINS(z.name, r'{excl}')
        AND EXTRACT(ISOYEAR FROM o.date) IN (2025, 2026)
        AND o.date < CURRENT_DATE('Asia/Seoul')
        AND NOT (
          EXTRACT(ISOYEAR FROM o.date) = EXTRACT(ISOYEAR FROM CURRENT_DATE('Asia/Seoul'))
          AND EXTRACT(ISOWEEK FROM o.date) >= EXTRACT(ISOWEEK FROM CURRENT_DATE('Asia/Seoul'))
        )
      GROUP BY 1, 2, 3, 4, 5
    ),
    way_raw AS (
      SELECT
        z.region_1 AS region1, {parent_col} AS region2_parent, {region_col} AS region,
        EXTRACT(ISOYEAR FROM r.return_at_kst) AS isoyear,
        EXTRACT(ISOWEEK  FROM r.return_at_kst) AS isoweek,
        CASE
          WHEN r.way = 'round'                     THEN '왕복'
          WHEN r.way IN ('d2d_oneway','d2d_round') THEN '부름'
          WHEN r.way = 'z2d_oneway'               THEN '편도'
          ELSE '기타'
        END AS way_type,
        COUNT(DISTINCT r.reservation_id) AS w_count,
        SUM(r.utime)               AS w_utime,
        SUM(r.revenue)             AS w_revenue,
        SUM(r.__rev_rent)          AS w_price,
        SUM(r.__rev_rent_discount) AS w_discount,
        COUNTIF(r.utime < 240)                      AS w_u_0to4h,
        COUNTIF(r.utime >= 240 AND r.utime < 480)   AS w_u_4to8h,
        COUNTIF(r.utime >= 480 AND r.utime < 1440)  AS w_u_8to24h,
        COUNTIF(r.utime >= 1440)                    AS w_u_24plus,
        SUM(IF(r.utime < 240,                    r.revenue, 0)) AS w_u_0to4h_rev,
        SUM(IF(r.utime >= 240 AND r.utime < 480, r.revenue, 0)) AS w_u_4to8h_rev,
        SUM(IF(r.utime >= 480 AND r.utime < 1440,r.revenue, 0)) AS w_u_8to24h_rev,
        SUM(IF(r.utime >= 1440,                  r.revenue, 0)) AS w_u_24plus_rev
      FROM `socar-data.socar_biz_profit.profit_socar_reservation` r
      INNER JOIN `socar-data.socar_zone.zone` z ON r.zone_id = z.legacy_zone_id
      INNER JOIN `socar-data.tianjin_replica.carzone_info` cz ON cz.id = z.legacy_zone_id
      WHERE {z_region}
        AND NOT REGEXP_CONTAINS(z.name, r'{excl}')
        AND EXTRACT(ISOYEAR FROM r.return_at_kst) IN (2025, 2026)
        AND r.return_at_kst < CAST(CURRENT_DATE('Asia/Seoul') AS DATETIME)
        AND NOT (
          EXTRACT(ISOYEAR FROM r.return_at_kst) = EXTRACT(ISOYEAR FROM CURRENT_DATE('Asia/Seoul'))
          AND EXTRACT(ISOWEEK  FROM r.return_at_kst) >= EXTRACT(ISOWEEK FROM CURRENT_DATE('Asia/Seoul'))
        )
      GROUP BY 1, 2, 3, 4, 5, 6
    ),
    way AS (
      SELECT
        region1, region2_parent, region, isoyear, isoweek,
        SAFE_DIVIDE(-SUM(w_discount), NULLIF(SUM(w_price),0))                AS discount_rate,
        SUM(IF(way_type='왕복', w_count,   0))                               AS round_count,
        SAFE_DIVIDE(SUM(IF(way_type='왕복', w_revenue, 0)),
                    NULLIF(SUM(IF(way_type='왕복', w_count, 0)),0))           AS round_rev_per_use,
        SAFE_DIVIDE(SUM(IF(way_type='왕복', w_utime, 0)),
                    NULLIF(SUM(IF(way_type='왕복', w_count, 0)),0)) / 60.0   AS round_avg_hours,
        SUM(IF(way_type='편도', w_count,   0))                               AS oneway_count,
        SAFE_DIVIDE(SUM(IF(way_type='편도', w_revenue, 0)),
                    NULLIF(SUM(IF(way_type='편도', w_count, 0)),0))           AS oneway_rev_per_use,
        SAFE_DIVIDE(SUM(IF(way_type='편도', w_utime, 0)),
                    NULLIF(SUM(IF(way_type='편도', w_count, 0)),0)) / 60.0   AS oneway_avg_hours,
        SUM(IF(way_type='부름', w_count,   0))                               AS d2d_count,
        SAFE_DIVIDE(SUM(IF(way_type='부름', w_revenue, 0)),
                    NULLIF(SUM(IF(way_type='부름', w_count, 0)),0))           AS d2d_rev_per_use,
        SAFE_DIVIDE(SUM(IF(way_type='부름', w_utime, 0)),
                    NULLIF(SUM(IF(way_type='부름', w_count, 0)),0)) / 60.0   AS d2d_avg_hours,
        SUM(w_u_0to4h)      AS u_0to4h,
        SUM(w_u_4to8h)      AS u_4to8h,
        SUM(w_u_8to24h)     AS u_8to24h,
        SUM(w_u_24plus)     AS u_24plus,
        SUM(w_u_0to4h_rev)  AS u_0to4h_rev,
        SUM(w_u_4to8h_rev)  AS u_4to8h_rev,
        SUM(w_u_8to24h_rev) AS u_8to24h_rev,
        SUM(w_u_24plus_rev) AS u_24plus_rev
      FROM way_raw GROUP BY 1, 2, 3, 4, 5
    ),
    payment AS (
      SELECT
        z.region_1 AS region1, {parent_col} AS region2_parent, {region_col} AS region,
        EXTRACT(ISOYEAR FROM DATE(v.timeMs, 'Asia/Seoul')) AS isoyear,
        EXTRACT(ISOWEEK  FROM DATE(v.timeMs, 'Asia/Seoul')) AS isoweek,
        COUNT(*) AS payment_attempt
      FROM `socar-data.socar_server_3.GET_CURRENT_CAR_RENTAL_VIEW` v
      INNER JOIN `socar-data.socar_zone.zone` z
             ON SAFE_CAST(v.carRental.startPoint.zoneId AS INT64) = z.legacy_zone_id
      INNER JOIN `socar-data.tianjin_replica.carzone_info` cz ON cz.id = z.legacy_zone_id
      WHERE {z_region}
        AND NOT REGEXP_CONTAINS(z.name, r'{excl}')
        AND v.carRental.startPoint.zoneId IS NOT NULL
        AND EXTRACT(ISOYEAR FROM DATE(v.timeMs, 'Asia/Seoul')) IN (2025, 2026)
        AND DATE(v.timeMs, 'Asia/Seoul') < CURRENT_DATE('Asia/Seoul')
        AND NOT (
          EXTRACT(ISOYEAR FROM DATE(v.timeMs, 'Asia/Seoul')) = EXTRACT(ISOYEAR FROM CURRENT_DATE('Asia/Seoul'))
          AND EXTRACT(ISOWEEK  FROM DATE(v.timeMs, 'Asia/Seoul')) >= EXTRACT(ISOWEEK FROM CURRENT_DATE('Asia/Seoul'))
        )
      GROUP BY 1, 2, 3, 4, 5
    ),
    demand AS (
      SELECT
        z.region_1 AS region1, {parent_col} AS region2_parent, {region_col} AS region,
        EXTRACT(ISOYEAR FROM DATE(l.start_at, 'Asia/Seoul')) AS isoyear,
        EXTRACT(ISOWEEK  FROM DATE(l.start_at, 'Asia/Seoul')) AS isoweek,
        COUNT(*) AS attempt,
        SAFE_DIVIDE(COUNTIF(l.available_car_count = 0), COUNT(*)) AS fail_rate
      FROM `socar-data.service_metrics.log_get_car_classes` l
      INNER JOIN `socar-data.socar_zone.zone` z ON l.zone_id = z.legacy_zone_id
      INNER JOIN `socar-data.tianjin_replica.carzone_info` cz ON cz.id = z.legacy_zone_id
      WHERE {z_region}
        AND NOT REGEXP_CONTAINS(z.name, r'{excl}')
        AND EXTRACT(ISOYEAR FROM DATE(l.start_at, 'Asia/Seoul')) IN (2025, 2026)
        AND DATE(l.start_at, 'Asia/Seoul') < CURRENT_DATE('Asia/Seoul')
        AND NOT (
          EXTRACT(ISOYEAR FROM DATE(l.start_at, 'Asia/Seoul')) = EXTRACT(ISOYEAR FROM CURRENT_DATE('Asia/Seoul'))
          AND EXTRACT(ISOWEEK  FROM DATE(l.start_at, 'Asia/Seoul')) >= EXTRACT(ISOWEEK FROM CURRENT_DATE('Asia/Seoul'))
        )
      GROUP BY 1, 2, 3, 4, 5
    )
    SELECT
      p.region1, p.region2_parent, p.region, p.isoyear, p.isoweek,
      CASE WHEN p.isoyear = EXTRACT(ISOYEAR FROM CURRENT_DATE('Asia/Seoul'))
           THEN 'now' ELSE 'yoy' END AS period_type,
      p.car_count, p.zone_count, p.revenue, p.profit, p.gpm, p.rev_per_car, p.use_count,
      IFNULL(u.opr_weekday, 0)     AS opr_weekday,
      IFNULL(u.opr_weekend, 0)     AS opr_weekend,
      IFNULL(w.discount_rate, 0)   AS discount_rate,
      IFNULL(w.round_count, 0)     AS round_count,
      IFNULL(w.round_rev_per_use, 0)  AS round_rev_per_use,
      IFNULL(w.round_avg_hours, 0)    AS round_avg_hours,
      IFNULL(w.oneway_count, 0)    AS oneway_count,
      IFNULL(w.oneway_rev_per_use, 0) AS oneway_rev_per_use,
      IFNULL(w.oneway_avg_hours, 0)   AS oneway_avg_hours,
      IFNULL(w.d2d_count, 0)      AS d2d_count,
      IFNULL(w.d2d_rev_per_use, 0)   AS d2d_rev_per_use,
      IFNULL(w.d2d_avg_hours, 0)     AS d2d_avg_hours,
      IFNULL(d.attempt, 0)           AS attempt,
      IFNULL(d.fail_rate, 0)         AS fail_rate,
      IFNULL(w.u_0to4h,      0)     AS u_0to4h,
      IFNULL(w.u_4to8h,      0)     AS u_4to8h,
      IFNULL(w.u_8to24h,     0)     AS u_8to24h,
      IFNULL(w.u_24plus,     0)     AS u_24plus,
      IFNULL(w.u_0to4h_rev,  0)     AS u_0to4h_rev,
      IFNULL(w.u_4to8h_rev,  0)     AS u_4to8h_rev,
      IFNULL(w.u_8to24h_rev, 0)     AS u_8to24h_rev,
      IFNULL(w.u_24plus_rev, 0)     AS u_24plus_rev,
      IFNULL(pay.payment_attempt, 0) AS payment_attempt
    FROM perf p
    LEFT JOIN util u USING (region1, region2_parent, region, isoyear, isoweek)
    LEFT JOIN way  w USING (region1, region2_parent, region, isoyear, isoweek)
    LEFT JOIN demand d USING (region1, region2_parent, region, isoyear, isoweek)
    LEFT JOIN payment pay USING (region1, region2_parent, region, isoyear, isoweek)
    WHERE p.region IS NOT NULL AND p.region != ''
    ORDER BY p.region1, p.region2_parent, p.region, p.isoyear, p.isoweek
    """
    try:
        rows = run_bq(sql, max_rows=50000)
    except Exception as e:
        print(f"  [경고] 지역별 실적 조회 실패 ({region_level}): {e}")
        return {'weekly': []}
    weekly = []
    for r in rows:
        weekly.append({
            'region1':         r.get('region1', ''),
            'region2_parent':  r.get('region2_parent') or '',
            'region':          r.get('region', ''),
            'isoyear':         int(r['isoyear']),
            'isoweek':         int(r['isoweek']),
            'period_type':     r['period_type'],
            'car_count':       round(float(r.get('car_count') or 0), 1),
            'zone_count':      int(float(r.get('zone_count') or 0)),
            'revenue':         int(float(r.get('revenue') or 0)),
            'profit':          int(float(r.get('profit') or 0)),
            'gpm':             round(float(r.get('gpm') or 0), 4),
            'rev_per_car':     round(float(r.get('rev_per_car') or 0)),
            'use_count':       int(float(r.get('use_count') or 0)),
            'opr_weekday':     round(float(r.get('opr_weekday') or 0), 4),
            'opr_weekend':     round(float(r.get('opr_weekend') or 0), 4),
            'discount_rate':   round(float(r.get('discount_rate') or 0), 4),
            'round_count':     int(float(r.get('round_count') or 0)),
            'round_rev_per_use': round(float(r.get('round_rev_per_use') or 0)),
            'round_avg_hours': round(float(r.get('round_avg_hours') or 0), 1),
            'oneway_count':    int(float(r.get('oneway_count') or 0)),
            'oneway_rev_per_use': round(float(r.get('oneway_rev_per_use') or 0)),
            'oneway_avg_hours': round(float(r.get('oneway_avg_hours') or 0), 1),
            'd2d_count':       int(float(r.get('d2d_count') or 0)),
            'd2d_rev_per_use': round(float(r.get('d2d_rev_per_use') or 0)),
            'd2d_avg_hours':   round(float(r.get('d2d_avg_hours') or 0), 1),
            'attempt':         int(float(r.get('attempt') or 0)),
            'fail_rate':       round(float(r.get('fail_rate') or 0), 4),
            'u_0to4h':         int(float(r.get('u_0to4h') or 0)),
            'u_4to8h':         int(float(r.get('u_4to8h') or 0)),
            'u_8to24h':        int(float(r.get('u_8to24h') or 0)),
            'u_24plus':        int(float(r.get('u_24plus') or 0)),
            'u_0to4h_rev':     int(float(r.get('u_0to4h_rev') or 0)),
            'u_4to8h_rev':     int(float(r.get('u_4to8h_rev') or 0)),
            'u_8to24h_rev':    int(float(r.get('u_8to24h_rev') or 0)),
            'u_24plus_rev':    int(float(r.get('u_24plus_rev') or 0)),
            'payment_attempt': int(float(r.get('payment_attempt') or 0)),
        })
    return {'weekly': weekly, 'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M')}


def haversine_km(lat1, lng1, lat2, lng2):
    """두 좌표 간 거리 (km)"""
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng/2)**2
    return R * 2 * math.asin(math.sqrt(a))


def _point_in_polygon(lat, lng, polygon):
    """Ray casting 알고리즘"""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        lat_i, lng_i = polygon[i]
        lat_j, lng_j = polygon[j]
        if ((lng_i > lng) != (lng_j > lng)) and \
           (lat < (lat_j - lat_i) * (lng - lng_i) / (lng_j - lng_i) + lat_i):
            inside = not inside
        j = i
    return inside


SEOUL_POLY = [
    (37.695, 126.764), (37.701, 126.886), (37.690, 126.952),
    (37.700, 127.020), (37.690, 127.092), (37.612, 127.115),
    (37.580, 127.165), (37.555, 127.185), (37.530, 127.180),
    (37.505, 127.170), (37.478, 127.145), (37.460, 127.110),
    (37.448, 127.045),
    (37.430, 126.990), (37.440, 126.905), (37.460, 126.835),
    (37.505, 126.775), (37.550, 126.764), (37.600, 126.790),
]

INCHEON_POLY = [
    (37.620, 126.540), (37.620, 126.680), (37.610, 126.730),
    (37.595, 126.755), (37.575, 126.770), (37.540, 126.780),
    (37.510, 126.785), (37.480, 126.785), (37.450, 126.775),
    (37.420, 126.760), (37.395, 126.750), (37.370, 126.730),
    (37.350, 126.700), (37.340, 126.640), (37.350, 126.580),
    (37.380, 126.540), (37.420, 126.510), (37.470, 126.500),
    (37.530, 126.510), (37.580, 126.520),
]


def is_in_seoul(lat, lng):
    """서울 행정경계 내부인지 판별"""
    return _point_in_polygon(lat, lng, SEOUL_POLY)


def is_in_incheon(lat, lng):
    """인천 행정경계 내부인지 판별"""
    return _point_in_polygon(lat, lng, INCHEON_POLY)


def is_non_gyeonggi(lat, lng):
    """서울 또는 인천 내부인지 판별"""
    return is_in_seoul(lat, lng) or is_in_incheon(lat, lng)


def filter_non_gyeonggi(data, lat_key='lat', lng_key='lng', zone_coords=None, keep_dist_km=1.0, team_id='gyeonggi'):
    """서울+인천 데이터 제거 (히트맵용: 경기존 접경은 허용). gyeonggi 이외 팀은 필터 스킵."""
    if team_id != 'gyeonggi':
        return data
    result = []
    for r in data:
        lat, lng = float(r[lat_key]), float(r[lng_key])
        if is_non_gyeonggi(lat, lng):
            if zone_coords:
                near = any(
                    abs(lat - zl) < 0.02 and abs(lng - zn) < 0.02
                    and haversine_km(lat, lng, zl, zn) <= keep_dist_km
                    for zl, zn in zone_coords
                )
                if near:
                    result.append(r)
        else:
            result.append(r)
    return result


def filter_strict_gyeonggi(data, lat_key='lat', lng_key='lng', team_id='gyeonggi'):
    """서울+인천 철저히 제외 (GAP/공급분석용: 접경 허용 없음). gyeonggi 이외 팀은 필터 스킵."""
    if team_id != 'gyeonggi':
        return data
    return [r for r in data if not is_non_gyeonggi(float(r[lat_key]), float(r[lng_key]))]


def _is_likely_gyeonggi(lat, lng):
    """경기도 범위 대략 판별 (강원/충남/인천 공항 등 제외)"""
    if is_non_gyeonggi(lat, lng):
        return False
    # 인천 공항/영종도 (lng < 126.55)
    if lng < 126.55:
        return False
    # 강원도 (lng > 127.55 and lat > 37.6)
    if lng > 127.55 and lat > 37.6:
        return False
    # 충청도 (lat < 37.0)
    if lat < 37.0:
        return False
    return True


def compute_gaps(access_data, reservation_data, zones_data, team_id='gyeonggi'):
    """앱 접속 있으나 반경 800m 내 존이 없는 지역 탐색 (팀 관할 행정구역 polygon 내부만)"""
    # 팀 관할 polygon 기반 필터 (인접 타 시도 좌표 제외)
    team_poly = _get_team_polygon(team_id)

    def _in_team(lat, lng):
        if team_id == 'gyeonggi' and not _is_likely_gyeonggi(lat, lng):
            return False
        if team_poly is not None and not team_poly.contains(Point(lng, lat)):
            return False
        return True

    # BQ에서 ROUND(lat,3) 단위로 이미 그루핑됨 → 같은 키로 매칭
    access_grid = {}
    for r in access_data:
        lat, lng = float(r['lat']), float(r['lng'])
        if not _in_team(lat, lng):
            continue
        key = f"{lat},{lng}"
        access_grid[key] = access_grid.get(key, 0) + int(r['access_count'])

    res_grid = {}
    for r in reservation_data:
        lat, lng = float(r['lat']), float(r['lng'])
        if not _in_team(lat, lng):
            continue
        key = f"{lat},{lng}"
        res_grid[key] = res_grid.get(key, 0) + int(r['reservation_count'])

    zone_coords = [(float(z['lat']), float(z['lng'])) for z in zones_data]

    # 접속 >= 90(3개월 누적) & 반경 500m 내 존 없는 곳 → gap 후보 선정
    MIN_ACCESS = 90
    MIN_DIST_KM = 0.3 if team_id == 'seoul' else 0.5 if team_id == 'gyeonggi' else 0.8
    RADIUS_KM = 0.5  # 수요 집계 반경 (시뮬레이션과 동일)
    num_months = 3

    # 격자 좌표 리스트 (반경 집계용)
    access_coords = [(float(k.split(',')[0]), float(k.split(',')[1]), v) for k, v in access_grid.items()]
    res_coords = [(float(k.split(',')[0]), float(k.split(',')[1]), v) for k, v in res_grid.items()]

    gaps = []
    for key, access_count in access_grid.items():
        if access_count < MIN_ACCESS:
            continue
        lat, lng = map(float, key.split(','))
        min_dist = min(haversine_km(lat, lng, zl, zn) for zl, zn in zone_coords)
        # 팀별 최대 거리: 서울은 존 밀도 높으므로 3km, 나머지는 10km
        max_gap_dist = 3.0 if team_id == 'seoul' else 10.0
        if min_dist > MIN_DIST_KM and min_dist < max_gap_dist:
            # 반경 500m bbox 내 접속/예약 합산 (시뮬레이션과 동일 기준)
            r500_access = sum(c for al, an, c in access_coords if haversine_km(lat, lng, al, an) <= RADIUS_KM)
            r500_res = sum(c for rl, rn, c in res_coords if haversine_km(lat, lng, rl, rn) <= RADIUS_KM)
            gaps.append({
                'lat': lat, 'lng': lng,
                'access_count': round(r500_access / num_months),
                'reservation_count': round(r500_res / num_months),
                'nearest_zone_km': round(min_dist, 2)
            })

    gaps.sort(key=lambda x: -x['access_count'])
    return gaps[:50]


def compute_reentry_zones(closed_data, access_data, reservation_data, zones_data=None, team_id='gyeonggi'):
    """폐쇄존 중 재진입 추천구역 필터링
    조건: 1) 평균 운영대수 >= 1, 2) 운영 30일 이상, 3) 반경 1km 내 접속/예약 있음, 4) 300m 내 운영존 없음
    """
    # 현재 운영 존 좌표
    active_zone_coords = []
    if zones_data:
        active_zone_coords = [(float(z['lat']), float(z['lng'])) for z in zones_data]
    # 접속/예약 격자 구축
    access_grid = {}
    for r in access_data:
        lat, lng = float(r['lat']), float(r['lng'])
        key = (round(lat, 3), round(lng, 3))
        access_grid[key] = access_grid.get(key, 0) + int(r['access_count'])

    res_grid = {}
    for r in reservation_data:
        lat, lng = float(r['lat']), float(r['lng'])
        key = (round(lat, 3), round(lng, 3))
        res_grid[key] = res_grid.get(key, 0) + int(r['reservation_count'])

    RADIUS_KM = 0.5  # 반경 500m (미진출지역/시뮬레이션과 동일)
    num_months = 3
    results = []
    # 부적합 존 이름 제외
    EXCLUDE_NAMES = ['공영주차장', '꼬끼오', '마트', '매각', '비즈니스', '시승', '장착', '캐리어', '캠핑카', '경매', '플랜']

    for z in closed_data:
        # 조건 0: 부적합 존 이름 제외
        zname = z.get('zone_name', '')
        if any(kw in zname for kw in EXCLUDE_NAMES):
            continue
        # 조건 1: 평균 운영대수 >= 1
        if float(z.get('hist_car_count', 0)) < 1:
            continue
        # 조건 2: 운영 30일 이상
        if int(z.get('operation_days', 0)) < 30:
            continue
        # 조건 2.5: 대당 매출 160만원 이상 (28일 기준)
        if float(z.get('revenue_per_car_28d', 0)) < 1600000:
            continue
        # 조건 3: 300m 내 운영 존 없음
        zlat, zlng = float(z['lat']), float(z['lng'])
        if active_zone_coords and any(haversine_km(zlat, zlng, al, an) <= 0.3 for al, an in active_zone_coords):
            continue
        # 조건 4: 반경 500m 내 접속/예약 유저 존재
        nearby_access = 0
        nearby_res = 0
        for (glat, glng), cnt in access_grid.items():
            if haversine_km(zlat, zlng, glat, glng) <= RADIUS_KM:
                nearby_access += cnt
        for (glat, glng), cnt in res_grid.items():
            if haversine_km(zlat, zlng, glat, glng) <= RADIUS_KM:
                nearby_res += cnt
        if nearby_access == 0 and nearby_res == 0:
            continue
        results.append({
            'zone_id': z.get('zone_id', ''),
            'zone_name': z.get('zone_name', ''),
            'parking_name': z.get('parking_name', ''),
            'lat': zlat, 'lng': zlng,
            'region1': _get_region1_by_polygon(zlat, zlng, team_id),
            'region2': z.get('region2', ''),
            'region3': z.get('region3', ''),
            'address': z.get('address', ''),
            'operation_days': int(z.get('operation_days', 0)),
            'hist_car_count': float(z.get('hist_car_count', 0)),
            'revenue_per_car_28d': float(z.get('revenue_per_car_28d', 0)),
            'gp_per_car_28d': float(z.get('gp_per_car_28d', 0)),
            'utilization_rate': float(z.get('utilization_rate', 0)),
            'first_date': z.get('first_date', ''),
            'last_date': z.get('last_date', ''),
            'nearby_access': round(nearby_access / num_months),
            'nearby_res': round(nearby_res / num_months),
        })

    # 주변 접속 월평균 100건 이상만
    results = [r for r in results if r['nearby_access'] >= 100]
    results.sort(key=lambda x: -(x['nearby_access'] + x['nearby_res']))
    return results[:50]


def query_advance_utilization(team_id='gyeonggi'):
    """사전가동률 조회 — 현재 주 스냅샷 기준 W+1, W+2 가동률 (now/yoy)
    스냅샷 테이블: socar_biz_base.operation_per_car_daily_v2_snapshot
    tight date filter 필수 (bytes billed 제한)
    """
    from datetime import date as ddate
    today = ddate.today()
    iso = today.isocalendar()
    curr_year, curr_week = iso.year, iso.week

    this_monday = ddate.fromisocalendar(curr_year, curr_week, 1)

    # 현재 W, W+1, W+2 날짜 범위
    w0_start = this_monday
    w0_end   = this_monday + timedelta(days=6)
    w1_start = this_monday + timedelta(weeks=1)
    w1_end   = this_monday + timedelta(weeks=2, days=-1)
    w2_start = this_monday + timedelta(weeks=2)
    w2_end   = this_monday + timedelta(weeks=3, days=-1)

    snap_now      = str(this_monday)
    snap_now_next = str(this_monday + timedelta(days=1))

    # YoY: 작년 동기 주차 월요일
    ly_year = curr_year - 1
    max_week_ly = ddate(ly_year, 12, 28).isocalendar().week
    ly_week = min(curr_week, max_week_ly)
    ly_monday = ddate.fromisocalendar(ly_year, ly_week, 1)

    ly_w0_start = ly_monday
    ly_w0_end   = ly_monday + timedelta(days=6)
    ly_w1_start = ly_monday + timedelta(weeks=1)
    ly_w1_end   = ly_monday + timedelta(weeks=2, days=-1)
    ly_w2_start = ly_monday + timedelta(weeks=2)
    ly_w2_end   = ly_monday + timedelta(weeks=3, days=-1)

    snap_yoy      = str(ly_monday)
    snap_yoy_next = str(ly_monday + timedelta(days=1))

    # socar_zone.zone 컬럼은 region_1 (언더스코어)
    regions = _expand_regions(TEAM_CONFIG[team_id]['regions'])
    r1_quoted = ', '.join(f"'{r}'" for r in regions)

    # region1 별칭 역매핑: zone 테이블 실제값 → 대시보드 표시명
    r1_norm_cases = '\n'.join(
        f"        WHEN z.region_1 = '{alias}' THEN '{canonical}'"
        for canonical, aliases in _REGION_ALIASES.items()
        for alias in aliases
        if alias != canonical
    )
    r1_norm_expr = f"CASE {r1_norm_cases}\n        ELSE z.region_1 END" if r1_norm_cases else "z.region_1"

    sql = f"""
    WITH base AS (
      SELECT 'now' AS period, ({r1_norm_expr}) AS region_1, z.region_2,
        CASE WHEN s.date BETWEEN '{w0_start}' AND '{w0_end}' THEN 'w0'
             WHEN s.date BETWEEN '{w1_start}' AND '{w1_end}' THEN 'w1'
             WHEN s.date BETWEEN '{w2_start}' AND '{w2_end}' THEN 'w2'
        END AS wk,
        CASE WHEN EXTRACT(DAYOFWEEK FROM s.date) IN (1,7) THEN 'weekend' ELSE 'weekday' END AS day_type,
        s.op_min, s.dp_min
      FROM `socar-data.socar_biz_base.operation_per_car_daily_v2_snapshot` s
      JOIN `socar-data.socar_zone.zone` z ON z.id = s.zone_id
      WHERE s.snapshot_at >= '{snap_now}' AND s.snapshot_at < '{snap_now_next}'
        AND s.date BETWEEN '{w0_start}' AND '{w2_end}'
        AND z.region_1 IN ({r1_quoted})
      UNION ALL
      SELECT 'yoy' AS period, ({r1_norm_expr}) AS region_1, z.region_2,
        CASE WHEN s.date BETWEEN '{ly_w0_start}' AND '{ly_w0_end}' THEN 'w0'
             WHEN s.date BETWEEN '{ly_w1_start}' AND '{ly_w1_end}' THEN 'w1'
             WHEN s.date BETWEEN '{ly_w2_start}' AND '{ly_w2_end}' THEN 'w2'
        END AS wk,
        CASE WHEN EXTRACT(DAYOFWEEK FROM s.date) IN (1,7) THEN 'weekend' ELSE 'weekday' END AS day_type,
        s.op_min, s.dp_min
      FROM `socar-data.socar_biz_base.operation_per_car_daily_v2_snapshot` s
      JOIN `socar-data.socar_zone.zone` z ON z.id = s.zone_id
      WHERE s.snapshot_at >= '{snap_yoy}' AND s.snapshot_at < '{snap_yoy_next}'
        AND s.date BETWEEN '{ly_w0_start}' AND '{ly_w2_end}'
        AND z.region_1 IN ({r1_quoted})
    ),
    grouped AS (
      -- region2별 (시/군/구)
      SELECT period, region_2, wk, day_type,
             SAFE_DIVIDE(SUM(op_min), SUM(dp_min)) AS util_rate
      FROM base WHERE wk IS NOT NULL
      GROUP BY period, region_2, wk, day_type
      UNION ALL
      -- region1별 (경기도/강원도 등) — region_2 컬럼에 region_1 값 저장
      SELECT period, region_1 AS region_2, wk, day_type,
             SAFE_DIVIDE(SUM(op_min), SUM(dp_min)) AS util_rate
      FROM base WHERE wk IS NOT NULL
      GROUP BY period, region_1, wk, day_type
      UNION ALL
      -- 팀 전체 (region_2 = '__all__')
      SELECT period, '__all__' AS region_2, wk, day_type,
             SAFE_DIVIDE(SUM(op_min), SUM(dp_min)) AS util_rate
      FROM base WHERE wk IS NOT NULL
      GROUP BY period, wk, day_type
    )
    SELECT * FROM grouped ORDER BY period, region_2, wk, day_type
    """

    rows = run_bq(sql, max_rows=2000)
    empty = lambda: {'weekday': None, 'weekend': None}
    by_region2 = {}  # '__all__' 포함

    for row in rows:
        period   = row.get('period')
        r2       = row.get('region_2') or '__all__'
        wk       = row.get('wk')
        day_type = row.get('day_type')
        val      = row.get('util_rate')
        if wk not in ('w0', 'w1', 'w2') or day_type not in ('weekday', 'weekend'):
            continue
        if r2 not in by_region2:
            by_region2[r2] = {
                'now': {'w0': empty(), 'w1': empty(), 'w2': empty()},
                'yoy': {'w0': empty(), 'w1': empty(), 'w2': empty()},
            }
        if val is not None and period in ('now', 'yoy'):
            try:
                by_region2[r2][period][wk][day_type] = float(val)
            except (ValueError, TypeError):
                pass

    total = by_region2.get('__all__', {'now': {'w0': empty(), 'w1': empty(), 'w2': empty()},
                                        'yoy': {'w0': empty(), 'w1': empty(), 'w2': empty()}})
    result = {
        'now': total['now'],
        'yoy': total['yoy'],
        'by_region2': by_region2,
        'meta': {
            'curr_week': curr_week,
            'curr_year': curr_year,
            'w0_label': f'W{curr_week}',
            'w1_label': f'W{curr_week + 1}',
            'w2_label': f'W{curr_week + 2}',
            'snap_date': snap_now,
            'ly_snap_date': snap_yoy,
        }
    }
    return result


def query_weekly_trends(team_id='gyeonggi'):
    """region2별 주간 접속/예약/공급 추이 (최근 12주)"""
    bb = _team_bbox(team_id)
    cz_region_filter = _region1_sql(team_id, 'cz')
    region_filter = _region1_sql(team_id)
    # 1) 접속: 주별 위치별 건수 → Python에서 region2 매핑
    access_sql = f"""
    WITH markers AS (
        SELECT DATE(timeMs, 'Asia/Seoul') AS dt,
            ROUND(location.lat, 2) AS lat, ROUND(location.lng, 2) AS lng
        FROM `socar-data.socar_server_3.GET_MARKERS_V2`
        WHERE timeMs >= TIMESTAMP('{THREE_MONTHS_AGO}', 'Asia/Seoul')
          AND timeMs < TIMESTAMP('{NEXT_DAY}', 'Asia/Seoul')
          AND location.lat BETWEEN {bb['lat'][0]} AND {bb['lat'][1]}
          AND location.lng BETWEEN {bb['lng'][0]} AND {bb['lng'][1]}
        UNION ALL
        SELECT DATE(timeMs, 'Asia/Seoul') AS dt,
            ROUND(location.lat, 2) AS lat, ROUND(location.lng, 2) AS lng
        FROM `socar-data.socar_server_3.GET_MARKERS`
        WHERE timeMs >= TIMESTAMP('{THREE_MONTHS_AGO}', 'Asia/Seoul')
          AND timeMs < TIMESTAMP('{NEXT_DAY}', 'Asia/Seoul')
          AND location.lat BETWEEN {bb['lat'][0]} AND {bb['lat'][1]}
          AND location.lng BETWEEN {bb['lng'][0]} AND {bb['lng'][1]}
    )
    SELECT FORMAT_DATE('%G-W%V', dt) AS week, lat, lng, COUNT(*) AS cnt
    FROM markers
    WHERE lat IS NOT NULL AND lng IS NOT NULL
    GROUP BY week, lat, lng
    ORDER BY week, cnt DESC
    """
    cmd = ["bq", "query", "--use_legacy_sql=false", "--format=json", "--max_rows=200000", access_sql]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(f"BQ access error: {result.stderr}")
    access_rows = json.loads(result.stdout)

    # 2) 예약: 주별 region2별 (존 소속 기준)
    res_sql = f"""
    SELECT FORMAT_DATE('%G-W%V', r.date) AS week, cz.region2,
        COUNT(DISTINCT r.reservation_id) AS res_cnt
    FROM `socar-data.soda_store.reservation_v2` r
    JOIN `socar-data.tianjin_replica.carzone_info` cz ON r.zone_id = cz.id
    WHERE r.date >= '{THREE_MONTHS_AGO}' AND r.date <= '{TODAY}'
      AND r.state = 3 AND r.member_imaginary IN (0,9)
      AND {cz_region_filter}
    GROUP BY week, cz.region2
    ORDER BY cz.region2, week
    """
    cmd = ["bq", "query", "--use_legacy_sql=false", "--format=json", "--max_rows=5000", res_sql]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(f"BQ res error: {result.stderr}")
    res_rows = json.loads(result.stdout)

    # 3) 공급: 주별 region2별
    supply_sql = f"""
    SELECT FORMAT_DATE('%G-W%V', date) AS week, region2,
        COUNT(DISTINCT car_id) AS car_cnt
    FROM `socar-data.socar_biz_profit.profit_socar_car_daily`
    WHERE {region_filter} AND car_state IN ('운영', '수리')
      AND car_sharing_type IN ('socar', 'zplus')
      AND date >= '{THREE_MONTHS_AGO}' AND date <= '{TODAY}'
    GROUP BY week, region2
    ORDER BY region2, week
    """
    cmd = ["bq", "query", "--use_legacy_sql=false", "--format=json", "--max_rows=5000", supply_sql]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(f"BQ supply error: {result.stderr}")
    supply_rows = json.loads(result.stdout)

    return {'access': access_rows, 'res': res_rows, 'supply': supply_rows}


def _assign_access_to_region(access_rows, zones_data):
    """접속 좌표를 가장 가까운 경기도 존의 region2에 할당 (그리드 캐시 사용)"""
    zone_coords = [(float(z['lat']), float(z['lng']), z.get('region2', '')) for z in zones_data]
    # 좌표→region2 캐시 (같은 ROUND(lat,2) 좌표는 같은 region)
    coord_cache = {}
    result = {}  # {(week, region2): count}
    for r in access_rows:
        lat, lng = float(r['lat']), float(r['lng'])
        coord_key = (lat, lng)
        if coord_key not in coord_cache:
            best_dist = float('inf')
            best_region = None
            for zl, zn, zr in zone_coords:
                if abs(lat - zl) > 0.15 or abs(lng - zn) > 0.15:
                    continue
                d = (lat - zl) ** 2 + (lng - zn) ** 2
                if d < best_dist:
                    best_dist = d
                    best_region = zr
            coord_cache[coord_key] = best_region
        region = coord_cache[coord_key]
        if region:
            key = (r['week'], region)
            result[key] = result.get(key, 0) + int(r['cnt'])
    return result


def _half_change(vals):
    """최근 절반 vs 이전 절반 평균 비교 (% 변화율)"""
    if len(vals) < 4:
        return 0
    mid = len(vals) // 2
    first_half = sum(vals[:mid]) / mid
    second_half = sum(vals[mid:]) / (len(vals) - mid)
    if first_half == 0:
        return 0
    return round((second_half - first_half) / first_half * 100, 1)


def compute_growth_analysis(weekly_data, zones_data=None, team_name=''):
    """주간 추이 데이터로 성장 지역 분석 + 공급 분류"""
    total_label = (team_name + ' 전체') if team_name else '경기강원팀 전체'
    # region2 → region1 매핑
    r2_to_r1 = {}
    if zones_data:
        for z in zones_data:
            r1 = z.get('region1', '')
            r2 = z.get('region2', '').replace('\u3000', ' ')
            if r2 and r1:
                r2_to_r1[r2] = r1
    # 접속 데이터 → region2 매핑
    if zones_data and isinstance(weekly_data, dict) and 'access' in weekly_data:
        access_by_wr = _assign_access_to_region(weekly_data['access'], zones_data)
    else:
        access_by_wr = {}

    # region2별 주간 데이터 정리
    by_region = {}
    all_weeks = set()

    # 접속 데이터
    for (week, rg), cnt in access_by_wr.items():
        if rg not in by_region:
            by_region[rg] = {}
        if week not in by_region[rg]:
            by_region[rg][week] = {'access': 0, 'res': 0, 'cars': 0}
        by_region[rg][week]['access'] += cnt
        all_weeks.add(week)

    # 예약 데이터
    res_rows = weekly_data.get('res', []) if isinstance(weekly_data, dict) else weekly_data
    for r in res_rows:
        rg = r['region2']
        week = r['week']
        if rg not in by_region:
            by_region[rg] = {}
        if week not in by_region[rg]:
            by_region[rg][week] = {'access': 0, 'res': 0, 'cars': 0}
        by_region[rg][week]['res'] = int(r.get('res_cnt', 0))
        all_weeks.add(week)

    # 공급 데이터
    supply_rows = weekly_data.get('supply', []) if isinstance(weekly_data, dict) else []
    for r in supply_rows:
        rg = r['region2']
        week = r['week']
        if rg not in by_region:
            by_region[rg] = {}
        if week not in by_region[rg]:
            by_region[rg][week] = {'access': 0, 'res': 0, 'cars': 0}
        by_region[rg][week]['cars'] = int(r.get('car_cnt', 0))
        all_weeks.add(week)

    # 현재 진행 중인 불완전한 주 제외
    from datetime import datetime as _dt
    current_week = _dt.now().strftime('%G-W%V')

    # 경기도 전체 주간 합계 (시즈널리티 기준선)
    gg_total = {}  # {week: {access, res, cars}}
    for rg, weeks_dict in by_region.items():
        for w, vals in weeks_dict.items():
            if w == current_week:
                continue
            if w not in gg_total:
                gg_total[w] = {'access': 0, 'res': 0, 'cars': 0}
            gg_total[w]['access'] += vals['access']
            gg_total[w]['res'] += vals['res']
            gg_total[w]['cars'] += vals['cars']

    sorted_all_weeks = sorted(w for w in gg_total.keys())

    analysis = []
    decline = []
    for rg, weeks_dict in by_region.items():
        sorted_weeks = sorted(w for w in weeks_dict.keys() if w != current_week and w in gg_total)
        if len(sorted_weeks) < 4:
            continue

        # 지역의 경기도 대비 점유율(%) 시계열 계산
        # 경기도 전체 합이 0인 주차는 해당 지표 계산에서 제외
        access_share = []
        res_share = []
        car_share = []
        for w in sorted_weeks:
            rv = weeks_dict[w]
            gt = gg_total[w]
            if gt['access'] > 0:
                access_share.append(rv['access'] / gt['access'] * 100)
            if gt['res'] > 0:
                res_share.append(rv['res'] / gt['res'] * 100)
            if gt['cars'] > 0:
                car_share.append(rv['cars'] / gt['cars'] * 100)

        access_vals = [weeks_dict[w]['access'] for w in sorted_weeks]
        res_vals = [weeks_dict[w]['res'] for w in sorted_weeks]
        car_vals = [weeks_dict[w]['cars'] for w in sorted_weeks]
        # 양 끝 불완전 주차 0값 트림
        while access_vals and access_vals[-1] == 0:
            access_vals.pop(); res_vals.pop() if res_vals else None; car_vals.pop() if car_vals else None
        while access_vals and access_vals[0] == 0:
            access_vals.pop(0); res_vals.pop(0) if res_vals else None; car_vals.pop(0) if car_vals else None

        if not access_vals or not res_vals:
            continue
        avg_access = sum(access_vals) / len(access_vals)
        avg_res = sum(res_vals) / len(res_vals)
        avg_cars = sum(car_vals) / len(car_vals) if car_vals else 0

        # 주평균
        access_weekly = round(avg_access)
        res_weekly = round(avg_res)
        cars_current = car_vals[-1] if car_vals else 0

        # 증감 = 경기도 대비 점유율의 후반기 vs 전반기 변화율
        access_growth = _half_change(access_share)
        res_growth = _half_change(res_share)
        car_growth = _half_change(car_share) if car_share else 0

        row = {
            'region2': rg,
            'region1': r2_to_r1.get(rg, ''),
            'access_weekly': access_weekly,
            'res_weekly': res_weekly,
            'cars': cars_current,
            'access_growth': access_growth,
            'res_growth': res_growth,
            'car_growth': car_growth,
            'access_trend': [round(v, 3) for v in access_share],
            'res_trend': [round(v, 3) for v in res_share],
            'car_trend': [round(v, 3) for v in car_share],
        }

        if access_growth > 0:
            # 수요 성장 (접속 점유율 증가): 공급 분류
            if car_growth < -1:
                row['status'] = '점검 필요'
            elif car_growth > 1:
                row['status'] = '대응 진행 중'
            else:
                row['status'] = '증차 검토'
            analysis.append(row)
        else:
            # 수요 감소 (접속 점유율 감소): 공급 분류
            if car_growth > 1:
                row['status'] = '점검 필요'
            elif car_growth < -1:
                row['status'] = '대응 진행 중'
            else:
                row['status'] = '감차 검토'
            decline.append(row)

    # 경기도 전체 시계열 (불완전 주차의 0값 제거)
    gg_access_vals = [gg_total[w]['access'] for w in sorted_all_weeks]
    gg_res_vals = [gg_total[w]['res'] for w in sorted_all_weeks]
    gg_car_vals = [gg_total[w]['cars'] for w in sorted_all_weeks]
    # 양 끝의 0 값 트림
    while gg_access_vals and gg_access_vals[-1] == 0:
        gg_access_vals.pop()
        if gg_res_vals: gg_res_vals.pop()
        if gg_car_vals: gg_car_vals.pop()
    while gg_access_vals and gg_access_vals[0] == 0:
        gg_access_vals.pop(0)
        if gg_res_vals: gg_res_vals.pop(0)
        if gg_car_vals: gg_car_vals.pop(0)
    gg_avg_access = sum(gg_access_vals) / len(gg_access_vals) if gg_access_vals else 0
    gg_avg_res = sum(gg_res_vals) / len(gg_res_vals) if gg_res_vals else 0
    gg_cars_current = gg_car_vals[-1] if gg_car_vals else 0

    gg_total_row = {
        'region2': total_label,
        'access_weekly': round(gg_avg_access),
        'res_weekly': round(gg_avg_res),
        'cars': gg_cars_current,
        'access_growth': _half_change(gg_access_vals),
        'res_growth': _half_change(gg_res_vals),
        'car_growth': _half_change(gg_car_vals) if gg_car_vals else 0,
        'status': '-',
        'access_trend': gg_access_vals,  # 경기도 전체는 절대값 트렌드
        'res_trend': gg_res_vals,
        'car_trend': gg_car_vals,
    }

    # 성장: 예약 점유율 상승 가파른 순 (내림차순)
    analysis.sort(key=lambda x: -x['res_growth'])

    # 감소: 예약 점유율 하락 가파른 순 (오름차순 = 가장 큰 음수부터)
    decline.sort(key=lambda x: x['res_growth'])

    # 경기도 전체를 첫 번째 요소로 삽입
    analysis.insert(0, gg_total_row)
    decline.insert(0, gg_total_row)
    return {'growth': analysis, 'decline': decline}


def reverse_geocode(gaps, team_id='gyeonggi', zones_data=None):
    """Nominatim 역지오코딩으로 한글 주소 변환"""
    gg_cities = ['수원','성남','용인','부천','안산','안양','남양주','화성','평택','의정부',
                 '시흥','파주','김포','광명','광주','군포','하남','오산','이천','양주',
                 '구리','안성','포천','의왕','여주','동두천','과천','가평','양평','연천','고양']
    non_gg = ['춘천','당진','홍천','음성','충주','원주','천안','아산','세종','인천','서울']

    results = []
    for g in gaps:
        url = f"https://nominatim.openstreetmap.org/reverse?lat={g['lat']}&lon={g['lng']}&format=json&accept-language=ko&zoom=16"
        req = urllib.request.Request(url, headers={'User-Agent': 'socar-demand-map/1.0'})
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
                data = json.loads(resp.read())
                addr = data.get('address', {})
                city = addr.get('city', addr.get('county', addr.get('town', '')))
                district = addr.get('borough', addr.get('city_district', ''))
                suburb = addr.get('suburb', addr.get('quarter', addr.get('neighbourhood', '')))
                village = addr.get('village', addr.get('hamlet', ''))
                parts = [p for p in [city, district, suburb or village] if p]
                name = ' '.join(parts) if parts else f"({g['lat']:.3f}, {g['lng']:.3f})"
        except Exception:
            name = f"({g['lat']:.3f}, {g['lng']:.3f})"
            addr = {}

        # 팀 소속 지역인지 확인
        province = addr.get('province', addr.get('state', ''))
        city = addr.get('city', addr.get('county', addr.get('town', '')))
        team_regions = TEAM_CONFIG[team_id]['regions']
        team_keywords = []
        for r in team_regions:
            for suffix in ['특별시','광역시','특별자치도','특별자치시','도']:
                if r.endswith(suffix):
                    team_keywords.append(r[:-len(suffix)])
                    break
            else:
                team_keywords.append(r[:2])

        # province 또는 city에서 팀 키워드 매칭
        check_str = (province or '') + ' ' + (city or '')
        is_team = any(kw in check_str for kw in team_keywords)
        # province/city 정보 없으면 bbox로 판단 (서울은 경기와 겹치므로 제외)
        if not is_team and not check_str.strip() and team_id != 'seoul':
            is_team = True
        # province 없고 다른 팀에도 매칭 안 되면 bbox 내 포함 (서울 제외)
        if not is_team and team_id != 'seoul':
            other_kws = []
            for tid, cfg in TEAM_CONFIG.items():
                if tid == team_id: continue
                for r in cfg['regions']:
                    for sfx in ['특별시','광역시','특별자치도','특별자치시','도']:
                        if r.endswith(sfx): other_kws.append(r[:-len(sfx)]); break
            if check_str.strip() and not any(kw in check_str for kw in other_kws):
                is_team = True
        if is_team:
            # region1: polygon 기반 확정 (Nominatim province 대신)
            gap_region1 = _get_region1_by_polygon(g['lat'], g['lng'], team_id)
            results.append({**g, 'name': name, 'region1': gap_region1})
        time.sleep(1.1)

    # 같은 이름의 인접 gap 병합 (접속/예약 합산, 좌표는 가중 평균)
    merged = {}
    for g in results:
        key = g['name']
        if key in merged:
            m = merged[key]
            total = m['access_count'] + g['access_count']
            if total > 0:
                w1, w2 = m['access_count'] / total, g['access_count'] / total
                m['lat'] = m['lat'] * w1 + g['lat'] * w2
                m['lng'] = m['lng'] * w1 + g['lng'] * w2
            m['access_count'] += g['access_count']
            m['reservation_count'] += g['reservation_count']
            m['nearest_zone_km'] = min(m['nearest_zone_km'], g['nearest_zone_km'])
        else:
            merged[key] = dict(g)
    results = sorted(merged.values(), key=lambda x: -x['access_count'])

    return results


# ── HTML Generation ────────────────────────────────────────────────────────

LEAFLET_CDN = """
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css" />
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css" />
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
"""

SHARED_STYLES = """
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f4f5f7; color: #1a1a1a; }
#map { position: absolute; top: 0; left: 260px; right: 0; bottom: 0; z-index: 0; }
.sidebar {
    position: fixed; top: 0; left: 0; bottom: 0; width: 260px; z-index: 1001;
    background: #ffffff; overflow-y: auto; display: flex; flex-direction: column;
    box-shadow: 2px 0 12px rgba(0,0,0,0.06);
}
.sidebar::-webkit-scrollbar { width: 4px; }
.sidebar::-webkit-scrollbar-track { background: transparent; }
.sidebar::-webkit-scrollbar-thumb { background: #d0d5dd; border-radius: 4px; }
.sidebar-header {
    padding: 20px 16px 14px;
    border-bottom: 1px solid #f0f1f3;
}
.sidebar-header h1 {
    font-size: 15px; font-weight: 800; color: #1a1a1a; margin-bottom: 6px;
    letter-spacing: -0.3px; display: flex; align-items: center; gap: 8px;
}
.sidebar-header h1 img.socar-logo {
    width: 22px; height: 22px; flex-shrink: 0; object-fit: contain;
}
.sidebar-header .date { font-size: 11px; color: #8b95a5; line-height: 1.6; }
.stat-row {
    display: flex; flex-wrap: wrap; gap: 6px; padding: 12px 16px;
    border-bottom: 1px solid #f0f1f3;
}
.stat-card {
    display: flex; flex-direction: column; align-items: center;
    background: #f7f8fa; border: none;
    border-radius: 10px; padding: 8px 8px; flex: 1; min-width: 65px;
    transition: all 0.2s;
}
.stat-card:hover { background: #eef0f4; }
.stat-card .label { font-size: 9px; color: #8b95a5; white-space: nowrap; margin-bottom: 2px; font-weight: 600; }
.stat-card .value { font-size: 14px; font-weight: 800; color: #1a1a1a; letter-spacing: -0.3px; }
.stat-card.socar .label { color: #0064FF; }
.stat-card.socar { background: #f0f4ff; }
.stat-card.socar .value { color: #0064FF; }
.stat-card.gcar .label { color: #ff8c00; }
.stat-card.gcar { background: #fff7ed; }
.stat-card.gcar .value { color: #e07800; }
.sidebar-section {
    padding: 14px 16px 8px; border-bottom: 1px solid #f0f1f3;
}
.sidebar-section-title {
    font-size: 10px; font-weight: 700; color: #8b95a5; text-transform: uppercase;
    letter-spacing: 1px; margin-bottom: 8px;
}
.sidebar-section select {
    width: 100%; padding: 8px 10px; border-radius: 10px; border: 1px solid #e8eaed;
    background: #f7f8fa; color: #1a1a1a; font-size: 12px; cursor: pointer;
    margin-bottom: 4px; transition: all 0.2s; font-weight: 500;
}
.sidebar-section select:focus { outline: none; border-color: #0064FF; box-shadow: 0 0 0 3px rgba(0,100,255,0.1); }
.sidebar-btn {
    display: flex; align-items: center; gap: 8px; width: 100%;
    padding: 9px 10px; margin-bottom: 2px; border-radius: 10px; border: none;
    background: transparent; color: #5a6270; font-size: 12px; cursor: pointer;
    transition: all 0.15s; text-align: left; font-weight: 600;
}
.sidebar-btn:hover { background: #f4f5f7; color: #1a1a1a; }
.sidebar-btn.active { background: #0064FF; color: #fff; }
.sidebar-btn .dot {
    width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0;
}
.sidebar-btn.active .dot { box-shadow: 0 0 0 2px rgba(255,255,255,0.4); }
.update-btn {
    display: flex; align-items: center; gap: 6px; width: 100%;
    padding: 10px 12px; margin-bottom: 4px; border-radius: 10px;
    border: none; background: #f7f8fa; color: #5a6270;
    font-size: 11px; font-weight: 600; cursor: pointer; transition: all 0.2s;
}
.update-btn:hover { background: #0064FF; color: #fff; }
.update-btn:disabled { opacity: 0.3; cursor: not-allowed; }
.update-btn.updating { animation: pulse 1.5s infinite; }
.update-time { font-size: 9px; color: #b0b8c4; padding: 2px 12px 4px; }
.analysis-tab { padding: 6px 14px; border-radius: 8px; border: 1px solid #e8eaed; background: #fff; color: #5a6270; font-size: 11px; cursor: pointer; transition: all 0.2s; font-weight: 600; }
.analysis-tab:hover { background: #f4f5f7; color: #1a1a1a; }
.analysis-tab.active { background: #0064FF; color: #fff; border-color: #0064FF; }
.search-tab { background: transparent; color: #8b95a5; }
.search-tab.active { background: #ffffff; color: #1a1a1a; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }
.search-tab:hover:not(.active) { color: #5a6270; }
@keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.3; } }
.legend-row {
    display: flex; flex-wrap: wrap; gap: 8px; padding: 12px 16px;
    border-top: 1px solid #f0f1f3; margin-top: auto;
    background: #fafbfc;
}
.legend-item { display: flex; align-items: center; gap: 4px; font-size: 9px; color: #8b95a5; font-weight: 600; }
.legend-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.gap-panel {
    position: fixed; top: 10px; right: 14px; z-index: 1000;
    background: #ffffff; border-radius: 16px;
    padding: 18px 20px; box-shadow: 0 4px 24px rgba(0,0,0,0.1);
    max-height: calc(100vh - 30px); overflow-y: auto; width: 340px;
    display: none; font-size: 12px; color: #1a1a1a;
}
.gap-panel h3 { font-size: 15px; margin-bottom: 10px; color: #1a1a1a; font-weight: 800; }
.gap-panel .gap-row {
    display: flex; justify-content: space-between; padding: 8px 6px;
    border-bottom: 1px solid #f0f1f3; cursor: pointer;
    border-radius: 6px; transition: all 0.15s;
}
.gap-panel .gap-row:hover { background: #f4f5f7; }
.ms-row:hover { background: #f0f4ff !important; }
.gap-panel .gap-name { color: #1a1a1a; flex: 1; font-weight: 500; }
.gap-panel .gap-cnt { color: #0064FF; font-weight: 700; min-width: 70px; text-align: right; }
.gap-panel table { color: #1a1a1a; }
.gap-panel th { color: #8b95a5; font-weight: 600; }
.gap-panel td { color: #1a1a1a; }
.leaflet-popup-content-wrapper {
    border-radius: 16px; background: #ffffff; color: #1a1a1a;
    box-shadow: 0 4px 24px rgba(0,0,0,0.12);
}
.leaflet-popup-tip { background: #ffffff; }
.leaflet-popup-content { font-size: 12px; line-height: 1.7; min-width: 220px; }
.popup-title {
    font-weight: 800; font-size: 15px; margin-bottom: 8px; padding-bottom: 8px;
    color: #1a1a1a; border-bottom: 2px solid #e8eaed;
}
.popup-row { display: flex; gap: 6px; align-items: center; padding: 2px 0; }
.popup-label { color: #8b95a5; min-width: 72px; font-size: 11px; flex-shrink: 0; font-weight: 600; }
.popup-section { border-top: 2px solid #e8eaed; margin-top: 8px; padding-top: 8px; }
.popup-section-title { font-weight: 700; font-size: 11px; color: #0064FF; margin-bottom: 5px; }
.popup-badge {
    display: inline-block; padding: 2px 10px; border-radius: 10px;
    font-size: 11px; font-weight: 600; color: #fff;
}
.heatmap-scale {
    position: fixed; bottom: 24px; right: 14px; z-index: 1000;
    display: flex; gap: 10px; background: #ffffff;
    border-radius: 14px; padding: 12px 16px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.1); font-size: 11px;
    color: #1a1a1a;
}
"""

TILE_SETUP = """
    var cartoDark = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
        attribution: '&copy; CartoDB', maxZoom: 19, subdomains: 'abcd'
    });
    var cartoVoyager = L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', {
        attribution: '&copy; CartoDB', maxZoom: 19, subdomains: 'abcd'
    });
    var vworldTile = L.tileLayer('https://xdworld.vworld.kr/2d/Base/service/{z}/{x}/{y}.png', {
        attribution: 'VWorld', maxZoom: 19
    });
    var baseLayers = { 'VWorld': vworldTile, '일반': cartoVoyager, '다크': cartoDark };
    vworldTile.addTo(map);
    L.control.layers(baseLayers, null, { position: 'bottomright', collapsed: true }).addTo(map);
"""

ZONE_JS = """
    function zoneColor(imaginary) {
        if (imaginary === 3) return 'rgba(0,100,255,0.75)';
        if (imaginary === 5) return 'rgba(142,68,173,0.7)';
        return 'rgba(39,174,96,0.55)';
    }
    function zoneLabel(imaginary) {
        if (imaginary === 3) return '스테이션';
        if (imaginary === 5) return '부름우선';
        return '일반';
    }
    function evCongestionChart(congestion) {
        if (!congestion) return '';
        var hasData = congestion.some(function(v) { return v >= 0; });
        if (!hasData) return '';
        var html = '<div style="margin-top:8px;">' +
            '<div style="font-size:9px;color:#8b95a5;font-weight:600;margin-bottom:4px;">충전 혼잡도 <span style="font-weight:400;">(수집 중)</span></div>' +
            '<div style="display:flex;align-items:flex-end;gap:1px;height:28px;">';
        congestion.forEach(function(v) {
            var h = v < 0 ? 2 : Math.max(2, v * 0.28);
            var color = v < 0 ? '#e8eaed' : v < 30 ? '#c8e6c9' : v < 60 ? '#81c784' : '#ef5350';
            html += '<div style="flex:1;height:' + h + 'px;background:' + color + ';border-radius:1px 1px 0 0;"></div>';
        });
        html += '</div><div style="display:flex;justify-content:space-between;margin-top:1px;">' +
            '<span style="font-size:8px;color:#aab0c4;">0</span><span style="font-size:8px;color:#aab0c4;">6</span>' +
            '<span style="font-size:8px;color:#aab0c4;">12</span><span style="font-size:8px;color:#aab0c4;">18</span>' +
            '<span style="font-size:8px;color:#aab0c4;">24</span></div>' +
            '<div style="display:flex;gap:6px;margin-top:2px;">' +
            '<span style="font-size:8px;color:#8b95a5;"><span style="display:inline-block;width:6px;height:6px;background:#c8e6c9;border-radius:1px;vertical-align:middle;margin-right:2px;"></span>여유 &lt;30%</span>' +
            '<span style="font-size:8px;color:#8b95a5;"><span style="display:inline-block;width:6px;height:6px;background:#81c784;border-radius:1px;vertical-align:middle;margin-right:2px;"></span>보통 30~60%</span>' +
            '<span style="font-size:8px;color:#8b95a5;"><span style="display:inline-block;width:6px;height:6px;background:#ef5350;border-radius:1px;vertical-align:middle;margin-right:2px;"></span>혼잡 &gt;60%</span>' +
            '</div></div>';
        return html;
    }

    function makeZoneIcon(z) {
        var color = zoneColor(z.imaginary);
        var size = Math.max(22, Math.min(38, 18 + z.car_count * 2));
        var label = z.car_count > 0 ? z.car_count : '';
        var evBadge = z.ev_zone ? '<div style="position:absolute;top:-6px;right:-6px;background:#1565c0;color:#fff;font-size:7px;font-weight:800;padding:1px 3px;border-radius:3px;white-space:nowrap;">EV</div>' : '';
        var html = '<div class="zone-pin" style="' +
            'width:' + size + 'px;height:' + size + 'px;' +
            'background:' + color + ';' +
            'position:relative;' +
            '">' + label + evBadge + '</div>';
        return L.divIcon({
            className: 'zone-marker-wrap',
            html: html,
            iconSize: [size, size],
            iconAnchor: [size/2, size/2],
            popupAnchor: [0, -size/2]
        });
    }
    function fmtNum(n) {
        if (!n || n === 0) return '-';
        return Math.round(n).toLocaleString();
    }
    function makePopup(z) {
        var d2d = z.is_d2d_car_exportable === 'ABLE' ? '부름 가능' : '부름 불가';
        var d2dColor = z.is_d2d_car_exportable === 'ABLE' ? '#27ae60' : '#e74c3c';
        var hasEv = !!z.ev_zone;
        var w = hasEv ? 'min-width:520px;' : '';

        // ── 왼쪽: 원본존 ──
        var left = '<div style="' + (hasEv ? 'flex:1;min-width:240px;' : '') + '">' +
            '<div class="popup-title">' + z.zone_name + '</div>' +
            '<div class="popup-row"><span class="popup-label">존 ID</span><span style="font-family:monospace;color:#aab0c4">' + z.zone_id + '</span></div>' +
            '<div class="popup-row"><span class="popup-label">주차장</span><span>' + z.parking_name + '</span></div>' +
            '<div class="popup-row"><span class="popup-label">주소</span><span>' + z.address + '</span></div>' +
            '<div class="popup-row"><span class="popup-label">차량</span><span><b>' + z.car_count + '</b>대</span></div>' +
            '<div class="popup-row"><span class="popup-label">유형</span><span class="popup-badge" style="background:' + zoneColor(z.imaginary) + '">' + zoneLabel(z.imaginary) + '</span></div>' +
            '<div class="popup-row"><span class="popup-label">부름</span><span style="color:' + d2dColor + ';font-weight:600">' + d2d + '</span></div>';
        if (z.provider_name || z.settlement_type || z.price_per_car) {
            left += '<div class="popup-section"><div class="popup-section-title">거래처 정보</div>' +
                (z.provider_name ? '<div class="popup-row"><span class="popup-label">사업자</span><span>' + z.provider_name + '</span></div>' : '') +
                (z.settlement_type ? '<div class="popup-row"><span class="popup-label">정산방식</span><span>' + z.settlement_type + '</span></div>' : '') +
                (z.price_per_car ? '<div class="popup-row"><span class="popup-label">대당 주차비</span><span><b>' + fmtNum(z.price_per_car) + '원</b>/월</span></div>' : '') +
                '</div>';
        }
        if (z.total_revenue > 0) {
            left += '<div class="popup-section"><div class="popup-section-title">실적 <span style="font-weight:400;font-size:10px;color:#8890a4;margin-left:4px;">최근 4주</span></div>' +
                '<div class="popup-row"><span class="popup-label">존 매출(4주)</span><b>' + fmtNum(z.total_revenue) + '원</b></div>' +
                '<div class="popup-row"><span class="popup-label">대당 매출</span><b>' + fmtNum(z.revenue_per_car_28d) + '원</b></div>' +
                '<div class="popup-row"><span class="popup-label">대당 GP</span><b style="color:' + (z.gp_per_car_28d < 0 ? '#e53935' : 'inherit') + '">' + fmtNum(z.gp_per_car_28d) + '원</b></div>' +
                '<div class="popup-row"><span class="popup-label">가동률</span><b>' + (z.utilization_rate || 0).toFixed(1) + '%</b></div>' +
                '</div>';
        }
        left += '</div>';

        // ── 충전기 정보 (공통 함수) ──
        function buildChargerHtml(evDetail) {
            if (!evDetail || evDetail.length === 0) return '<div style="font-size:10px;color:#bbb;font-weight:600;">⚡ 100m 내 충전기 없음</div>';
            var html = '';
            evDetail.forEach(function(e, i) {
                var label = (e.fast > 0 ? '급속' + e.fast : '') + (e.fast > 0 && e.slow > 0 ? '+' : '') + (e.slow > 0 ? '완속' + e.slow : '');
                var title = i === 0 ? '⚡ 최인근 충전기' : '';
                if (title) html += '<div style="font-size:10px;color:#43a047;font-weight:700;margin-bottom:4px;">' + title + '</div>';
                html += '<div class="popup-row"><span class="popup-label" style="font-size:10px;color:#43a047">' + e.dist + 'm</span><span style="font-size:10px">' + e.name + ' <b>' + e.total + '기</b> <span style="color:#8b95a5;font-size:9px">(' + label + ')</span></span></div>';
                if (i === 0 && e.congestion) html += evCongestionChart(e.congestion);
                if (i === 0 && evDetail.length > 1) html += '<div style="font-size:10px;color:#8b95a5;font-weight:600;margin-top:6px;margin-bottom:2px;">기타 충전소</div>';
            });
            return html;
        }

        // ── 오른쪽 칼럼 ──
        var hasCharger = z.ev_detail && z.ev_detail.length > 0;
        var hasRightCol = hasEv || hasCharger;
        var w = hasRightCol ? 'min-width:520px;' : '';

        var right = '';
        if (hasRightCol) {
            right = '<div style="flex:1;min-width:220px;border-left:2px solid ' + (hasEv ? '#1565c0' : '#43a047') + ';padding-left:14px;">';
            if (hasEv) {
                var ev = z.ev_zone;
                var evD2d = ev.is_d2d ? '부름 가능' : '부름 불가';
                var evD2dColor = ev.is_d2d ? '#27ae60' : '#e74c3c';
                right += '<div style="display:flex;align-items:center;gap:6px;margin-bottom:8px;"><span style="background:#1565c0;color:#fff;font-size:9px;font-weight:800;padding:2px 6px;border-radius:3px;">충전보장 EV</span></div>' +
                    '<div class="popup-title" style="font-size:13px;">' + ev.zone_name + '</div>' +
                    '<div class="popup-row"><span class="popup-label">존 ID</span><span style="font-family:monospace;color:#aab0c4">' + ev.zone_id + '</span></div>' +
                    '<div class="popup-row"><span class="popup-label">차량</span><span><b>' + ev.car_count + '</b>대</span></div>' +
                    '<div class="popup-row"><span class="popup-label">유형</span><span class="popup-badge" style="background:' + zoneColor(ev.imaginary) + '">' + zoneLabel(ev.imaginary) + '</span></div>' +
                    '<div class="popup-row"><span class="popup-label">부름</span><span style="color:' + evD2dColor + ';font-weight:600">' + evD2d + '</span></div>';
                if (ev.total_revenue > 0) {
                    right += '<div class="popup-section"><div class="popup-section-title" style="color:#1565c0;">실적 <span style="font-weight:400;font-size:10px;color:#8890a4;margin-left:4px;">최근 4주</span></div>' +
                        '<div class="popup-row"><span class="popup-label">존 매출(4주)</span><b>' + fmtNum(ev.total_revenue) + '원</b></div>' +
                        '<div class="popup-row"><span class="popup-label">대당 매출</span><b>' + fmtNum(ev.revenue_per_car_28d) + '원</b></div>' +
                        '<div class="popup-row"><span class="popup-label">대당 GP</span><b style="color:' + (ev.gp_per_car_28d < 0 ? '#e53935' : 'inherit') + '">' + fmtNum(ev.gp_per_car_28d) + '원</b></div>' +
                        '<div class="popup-row"><span class="popup-label">가동률</span><b>' + (ev.utilization_rate || 0).toFixed(1) + '%</b></div>' +
                        '</div>';
                }
                right += '<div style="border-top:1px solid #f0f1f3;margin:6px 0;"></div>';
            }
            right += buildChargerHtml(z.ev_detail);
            right += '</div>';
        }

        var evZid = z.ev_zone ? z.ev_zone.zone_id : 0;
        var evZnm = z.ev_zone ? z.ev_zone.zone_name.replace(/"/g,'') : '';
        var bottom = '<div class="popup-section" style="display:flex;gap:6px;">' +
            '<button onclick="showD2dDestinations(' + z.zone_id + ',&quot;' + z.zone_name.replace(/"/g,'') + '&quot;,' + evZid + ')" style="flex:1;padding:8px;border:none;border-radius:8px;background:#0064FF;color:#fff;font-size:11px;font-weight:600;cursor:pointer;">부름호출지역</button>' +
            '<button onclick="showTimeline(' + z.zone_id + ',&quot;' + z.zone_name.replace(/"/g,'') + '&quot;,' + (evZid || 'null') + ',' + (evZnm ? '&quot;' + evZnm + '&quot;' : 'null') + ')" style="flex:1;padding:8px;border:none;border-radius:8px;background:#5c6bc0;color:#fff;font-size:11px;font-weight:600;cursor:pointer;">예약 현황</button>' +
            '</div>';

        if (hasRightCol) {
            return '<div style="' + w + '"><div style="display:flex;gap:14px;">' + left + right + '</div>' + bottom + '</div>';
        }
        return left + bottom;
    }
"""


def jd(data):
    return json.dumps(data, ensure_ascii=False)


def _read_ngrok_url():
    try:
        with open(NGROK_URL_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ''


def generate_index(access_data, reservation_data, zones_data, gaps, analysis=None, dtod_data=None, profit_data=None, profit_period='', gcar_data=None, socar_supply=None, parking_contract=None, timeline_data=None, closed_data=None, reentry_data=None, ev_data=None, team_id='gyeonggi'):
    """잠재 수요 지도 HTML 생성"""
    team_name = TEAM_CONFIG[team_id]['name']
    team_center = TEAM_CONFIG[team_id]['center']
    team_zoom = TEAM_CONFIG[team_id]['zoom']
    if analysis is None:
        analysis = {}
    # analysis가 dict(growth/decline)이면 분리, 아니면 하위호환
    if isinstance(analysis, dict):
        analysis_growth = analysis.get('growth', [])
        analysis_decline = analysis.get('decline', [])
    else:
        analysis_growth = analysis
        analysis_decline = []
    if dtod_data is None:
        dtod_data = []
    if profit_data is None:
        profit_data = {}
    if gcar_data is None:
        gcar_data = []
    if socar_supply is None:
        socar_supply = {}
    if parking_contract is None:
        parking_contract = {}
    if parking_contract:
        parking_contract = {int(k): v for k, v in parking_contract.items()}
    if timeline_data is None:
        timeline_data = {}
    if closed_data is None:
        closed_data = []
    # '구'로 끝나는 region2에 상위 시/도 prefix 추가 (영도구→부산 영도구)
    # '시' 하위 '구'는 이미 "창원시 성산구" 형태이므로 제외
    def _prefix_region2(r1, r2):
        r2 = r2.replace('\u3000', ' ')
        if r2.endswith('구') and ' ' not in r2:
            r1_short = r1.replace('특별시','').replace('광역시','').replace('특별자치도','').replace('특별자치시','').replace('도','')
            return f'{r1_short} {r2}'
        return r2

    # profit + 주차 계약 데이터를 zones_data에 병합
    for z in zones_data:
        zid = int(z.get('zone_id', 0))
        z['zone_id'] = zid
        z['imaginary'] = int(z.get('imaginary', 0) or 0)
        z['car_count'] = int(z.get('car_count', 0) or 0)
        z['lat'] = float(z.get('lat', 0))
        z['lng'] = float(z.get('lng', 0))
        # region2에 상위 시/도 prefix 추가
        z['region2'] = _prefix_region2(z.get('region1', ''), z.get('region2', ''))
        p = profit_data.get(zid, {})
        z['total_revenue'] = p.get('total_revenue', 0)
        z['revenue_per_car_28d'] = p.get('revenue_per_car_28d', 0)
        z['gp_per_car_28d'] = p.get('gp_per_car_28d', 0)
        z['utilization_rate'] = p.get('utilization_rate', 0)
        pc = parking_contract.get(zid, {})
        z['provider_name'] = pc.get('provider_name', '')
        z['settlement_type'] = pc.get('settlement_type', '')
        z['price_per_car'] = pc.get('price_per_car', 0)
    # 주평균 계산 (90일 / 7)
    num_weeks = 90 / 7
    access_heat = [[float(r['lat']), float(r['lng']), round(int(r['access_count']) / num_weeks)] for r in access_data]
    res_heat = [[float(r['lat']), float(r['lng']), round(int(r['reservation_count']) / num_weeks)] for r in reservation_data]
    dtod_dots = [[float(r['lat']), float(r['lng']), int(r['call_count'])] for r in dtod_data]

    # gcar 데이터 변환
    gcar_zones = []
    for g in gcar_data:
        sig = g.get('sig_name', '')
        r1_from_sig = sig.split()[0] if sig else ''
        r2 = _prefix_region2(r1_from_sig, g.get('region2', ''))
        gcar_zones.append({
            'zone_id': int(g.get('zone_id', 0)),
            'zone_name': g.get('zone_name', ''),
            'region2': r2,
            'lat': float(g.get('lat', 0)),
            'lng': float(g.get('lng', 0)),
            'total_cars': int(g.get('total_cars', 0)),
            'sig_name': g.get('sig_name', ''),
        })

    # 폐쇄 존 데이터 변환 + parking_contract 병합
    closed_zones = []
    for cz in closed_data:
        zid = int(cz.get('zone_id', 0))
        pc = parking_contract.get(zid, {})
        closed_zones.append({
            'zone_id': zid,
            'zone_name': cz.get('zone_name', ''),
            'parking_name': cz.get('parking_name', ''),
            'lat': float(cz.get('lat', 0)),
            'lng': float(cz.get('lng', 0)),
            'region2': cz.get('region2', ''),
            'address': cz.get('address', ''),
            'state': int(cz.get('state', 0)),
            'first_date': str(cz.get('first_date', '')),
            'last_date': str(cz.get('last_date', '')),
            'operation_days': int(cz.get('operation_days', 0)),
            'revenue_per_car_28d': int(float(cz.get('revenue_per_car_28d', 0) or 0)),
            'gp_per_car_28d': int(float(cz.get('gp_per_car_28d', 0) or 0)),
            'utilization_rate': float(cz.get('utilization_rate', 0) or 0),
            'hist_car_count': float(cz.get('hist_car_count', 0) or 0),
            'provider_name': pc.get('provider_name', ''),
            'settlement_type': pc.get('settlement_type', ''),
            'price_per_car': pc.get('price_per_car', 0),
        })

    # Market share 분석: region2별 쏘카(zones_data 현재 기준) vs 그린카 차량수 비교
    socar_by_region = {}
    for z in zones_data:
        r2 = z.get('region2', '')
        if not r2:
            continue
        socar_by_region.setdefault(r2, {'cars': 0, 'zones': 0})
        socar_by_region[r2]['cars'] += z.get('car_count', 0)
        socar_by_region[r2]['zones'] += 1
    gcar_by_region = {}
    for g in gcar_zones:
        r2 = g['region2'].replace('\u3000', ' ')
        gcar_by_region.setdefault(r2, {'cars': 0, 'zones': 0})
        gcar_by_region[r2]['cars'] += g['total_cars']
        gcar_by_region[r2]['zones'] += 1
    all_regions_ms = sorted(set(list(socar_by_region.keys()) + list(gcar_by_region.keys())))
    market_share = []
    for r2 in all_regions_ms:
        s = socar_by_region.get(r2, {'cars': 0, 'zones': 0})
        g = gcar_by_region.get(r2, {'cars': 0, 'zones': 0})
        sc = s.get('cars', 0)
        sz = s.get('zones', 0)
        gc = g['cars']
        gz = g['zones']
        total = sc + gc
        if total == 0:
            continue
        market_share.append({
            'region2': r2,
            'socar_cars': sc,
            'socar_zones': sz,
            'gcar_cars': gc,
            'gcar_zones': gz,
            'total_cars': total,
            'socar_share': round(sc / total * 100, 1) if total > 0 else 0,
        })
    market_share.sort(key=lambda x: x['region2'])

    total_a = round(sum(int(r['access_count']) for r in access_data) / num_weeks)
    total_r = round(sum(int(r['reservation_count']) for r in reservation_data) / num_weeks)
    total_d = round(sum(int(r['call_count']) for r in dtod_data) / num_weeks)
    total_z = len(zones_data)
    total_cars = sum(int(z.get('car_count', 0)) for z in zones_data)
    total_gcar_z = len(gcar_zones)
    total_gcar_cars = sum(g['total_cars'] for g in gcar_zones)
    total_closed_z = len(closed_zones)

    regions = sorted(set(z.get('region2', '') for z in zones_data if z.get('region2')))

    LAST_UPDATE_DEMAND = _read_last_update(LAST_UPDATE_DEMAND_FILE)
    LAST_UPDATE_ZONE = _read_last_update(LAST_UPDATE_ZONE_FILE)
    ngrok_url = _read_ngrok_url()

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{team_name} 수요/인프라 지도</title>
{LEAFLET_CDN}
<style>
{SHARED_STYLES}
.zone-marker-wrap {{ background: none !important; border: none !important; }}
.zone-pin {{
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    color: #fff; font-size: 10px; font-weight: 700;
    box-shadow: 0 1px 4px rgba(0,0,0,0.2);
    border: 1.5px solid rgba(255,255,255,0.7);
    transition: transform 0.15s, box-shadow 0.15s, opacity 0.15s;
    cursor: pointer;
}}
.zone-pin:hover {{
    transform: scale(1.2);
    opacity: 1 !important;
    box-shadow: 0 3px 10px rgba(0,0,0,0.35);
}}
.scale-col {{ display: flex; flex-direction: column; align-items: center; gap: 2px; }}
.scale-col .scale-title {{ font-weight: 700; font-size: 10px; margin-bottom: 2px; }}
.scale-col .scale-bar {{ width: 18px; height: 120px; border-radius: 4px; }}
.scale-col .scale-labels {{ display: flex; flex-direction: column; justify-content: space-between; height: 120px; font-size: 9px; color: #8890a4; }}
.scale-row {{ display: flex; gap: 4px; align-items: stretch; }}
.sim-ctx {{
    position: absolute; z-index: 2000; background: #ffffff;
    border-radius: 12px; padding: 4px 0; box-shadow: 0 4px 20px rgba(0,0,0,0.12); display: none;
}}
.sim-ctx-item {{
    padding: 10px 18px; font-size: 12px; color: #5a6270; cursor: pointer; white-space: nowrap;
    transition: all 0.15s; font-weight: 600;
}}
.sim-ctx-item:hover {{ background: #f4f5f7; color: #1a1a1a; }}
.sim-panel {{
    position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%); z-index: 2000;
    background: #ffffff; border: none; border-radius: 20px;
    padding: 28px 32px; box-shadow: 0 16px 48px rgba(0,0,0,0.15); width: 1100px; max-width: 95vw; max-height: 90vh; overflow-y: auto;
    display: none; color: #1a1a1a; font-size: 12px;
}}
.sim-body {{ display: flex; gap: 24px; }}
.sim-left {{ flex: 1; min-width: 0; }}
.sim-right {{
    width: 320px; flex-shrink: 0; background: #f7f8fa;
    border-radius: 12px; padding: 18px 20px;
    font-size: 13px; color: #5a6270; line-height: 1.75;
}}
.sim-right h4 {{ font-size: 14px; font-weight: 800; color: #1a1a1a; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid #f0f1f3; }}
.sim-right .cr-section {{ margin-bottom: 14px; }}
.sim-right .cr-title {{ font-size: 13px; font-weight: 700; color: #0064FF; margin-bottom: 5px; }}
.sim-right code {{ background: #f0f4ff; padding: 2px 7px; border-radius: 4px; font-size: 12px; color: #0064FF; }}
.sim-panel h3 {{ font-size: 16px; font-weight: 800; color: #1a1a1a; margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid #f0f1f3; }}
.sim-row {{ display: flex; justify-content: space-between; align-items: center; padding: 5px 0; }}
.sim-row .sim-label {{ color: #8b95a5; font-size: 11px; font-weight: 600; }}
.sim-row .sim-value {{ font-weight: 800; color: #1a1a1a; font-size: 13px; }}
.sim-section {{ border-top: 1px solid #f0f1f3; margin-top: 10px; padding-top: 10px; }}
.sim-section-title {{ font-size: 10px; font-weight: 700; color: #8b95a5; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }}
.sim-result {{
    margin-top: 14px; padding: 14px 18px; border-radius: 12px; text-align: center;
    font-size: 14px; font-weight: 800;
}}
.sim-result.recommend {{ background: #edfcf2; border: none; color: #18a34a; }}
.sim-result.not-recommend {{ background: #fef2f2; border: none; color: #e74c3c; }}
.sim-close {{
    position: absolute; top: 14px; right: 16px; background: #f4f5f7; border: none;
    color: #8b95a5; font-size: 16px; cursor: pointer; line-height: 1; transition: all 0.2s;
    width: 28px; height: 28px; border-radius: 50%; display: flex; align-items: center; justify-content: center;
}}
.sim-close:hover {{ background: #e8eaed; color: #1a1a1a; }}
.sim-overlay {{
    position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.3);
    z-index: 1999; display: none; backdrop-filter: blur(4px);
}}
.sim-marker-pin {{
    width: 32px; height: 32px; border-radius: 50%; background: #0064FF;
    border: 2px dashed #fff; display: flex; align-items: center; justify-content: center;
    color: #fff; font-size: 14px; font-weight: 700; animation: sim-pulse 1.5s infinite;
    box-shadow: 0 2px 8px rgba(0,100,255,0.3);
}}
@keyframes sim-pulse {{ 0%,100% {{ box-shadow: 0 0 0 0 rgba(0,100,255,0.4); }} 50% {{ box-shadow: 0 0 0 12px rgba(0,100,255,0); }} }}
</style>
</head>
<body>

<div class="sidebar">
    <div class="sidebar-header">
        <h1><img class="socar-logo" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACwAAAAsCAIAAACR5s1WAAABY2lDQ1BrQ0dDb2xvclNwYWNlRGlzcGxheVAzAAAokX2QsUvDUBDGv1aloHUQHRwcMolDlJIKuji0FURxCFXB6pS+pqmQxkeSIgU3/4GC/4EKzm4Whzo6OAiik+jm5KTgouV5L4mkInqP435877vjOCA5bnBu9wOoO75bXMorm6UtJfWMBL0gDObxnK6vSv6uP+P9PvTeTstZv///jcGK6TGqn5QZxl0fSKjE+p7PJe8Tj7m0FHFLshXyieRyyOeBZ71YIL4mVljNqBC/EKvlHt3q4brdYNEOcvu06WysyTmUE1jEDjxw2DDQhAId2T/8s4G/gF1yN+FSn4UafOrJkSInmMTLcMAwA5VYQ4ZSk3eO7ncX3U+NtYMnYKEjhLiItZUOcDZHJ2vH2tQ8MDIEXLW54RqB1EeZrFaB11NguASM3lDPtlfNauH26Tww8CjE2ySQOgS6LSE+joToHlPzA3DpfAEDp2ITpJYOWwAAAARjSUNQDA0AAW4D4+8AAAA4ZVhJZk1NACoAAAAIAAGHaQAEAAAAAQAAABoAAAAAAAKgAgAEAAAAAQAAACygAwAEAAAAAQAAACwAAAAA36bFmQAAAZ1pVFh0WE1MOmNvbS5hZG9iZS54bXAAAAAAADx4OnhtcG1ldGEgeG1sbnM6eD0iYWRvYmU6bnM6bWV0YS8iIHg6eG1wdGs9IlhNUCBDb3JlIDYuMC4wIj4KICAgPHJkZjpSREYgeG1sbnM6cmRmPSJodHRwOi8vd3d3LnczLm9yZy8xOTk5LzAyLzIyLXJkZi1zeW50YXgtbnMjIj4KICAgICAgPHJkZjpEZXNjcmlwdGlvbiByZGY6YWJvdXQ9IiIKICAgICAgICAgICAgeG1sbnM6ZXhpZj0iaHR0cDovL25zLmFkb2JlLmNvbS9leGlmLzEuMC8iPgogICAgICAgICA8ZXhpZjpQaXhlbFhEaW1lbnNpb24+NjAwPC9leGlmOlBpeGVsWERpbWVuc2lvbj4KICAgICAgICAgPGV4aWY6UGl4ZWxZRGltZW5zaW9uPjM3NDwvZXhpZjpQaXhlbFlEaW1lbnNpb24+CiAgICAgIDwvcmRmOkRlc2NyaXB0aW9uPgogICA8L3JkZjpSREY+CjwveDp4bXBtZXRhPgpUzMdaAAAC+ElEQVRYCe2XTWsTQRjH55ndnd1N0qTG2iqhYhGkvqAURS8KFS+CiAfvPYoXP4PgVxDvFQS/gAcVPHhpRaVIoaUKfSGIpW/20LRJdmfm8dkkKzkIzUzRetg5ZJckM/Ob////zOwCIrLDbvywAZL5M4jUhUyJTIlUgfSaZeK/UsJNaYyv1TX1airiHBhLNn6l4dZld/SkzYA2fdq8rz/GTyYxDBmdPgBQb6LS8l9DbOxgvsQC0T7/EASWip6xnq0O9kpU1zHwQaTzIuBQ2Y6BWUJs19QSQQTAWzVOahQ8NjxgWfCWEDOLamuPiYCWTgCgEAeLODxIIbVplhBvZpTjcdfrzKpidmGEF0PHBoFZ2TFbjT8tqzxNCa1UIgMH7l5N02EOYqyE1Pj8vdTcFUnXxAsGIKJo+nNj7qvQwLXCWOLNMXHxdK9YxhBvZ+MvVczloOVE6wNxZWH32RwXeUcpJhXfrePqz/hvQWzsqJcfZBBwp1MHQEZsVmuyofuPhsxxpWSoIZfjt68Z5MNACXosfzEdbe5BukGRDyzak5vf62ExdH1Ha8KARhPvX2fjl0Tv2TCAWFyP380rh7sUi/YErsNWl2qe5/l9AriDCkmg4SH96F5Su703A4icz0dPgJJUDAkEd2B5pbGzLfsH+zzBFVUa5xHDh3fc42UDL2goMHr5IQl+/19pnHi8NrfmlQZCygiZFUk2NsKePvC9NDI9imGgRLJ6qoZ2WTA2v9RYWIXSMd/zk5QShACcGPdMCaivGUT3yqYWYhb4Qd4F0h4hVmy0wq6cMTOiPaA9xLcfunBEuKIjjUS8cY4LQyMOBBFJvdWAQpEL0Tk+uMvGTrVlaG2j3aLtd2+pxG6TNZgnfHBa82rEco5VyhQOYwIitISoR9hE0FRcip7tUGoIAywESW73W/YffreEyPlwtsJqTZ0UBiCl8nwFPJtQJkxm+0T3KsiCdOekr9HhVLs2MhwIohvogPeJnIfeMojUgkyJTIlUgfSaZSJV4heBdAXfF6Z6qQAAAABJRU5ErkJggg==" alt="SOCAR">{team_name} 수요/인프라 지도</h1>
        <div class="date">{THREE_MONTHS_AGO} ~ {TODAY}</div>
        <div class="date">업데이트: {TODAY}</div>
    </div>
    <div class="sidebar-section">
        <div class="sidebar-section-title">검색</div>
        <div style="display:flex;gap:0;margin-bottom:8px;background:#f4f5f7;border-radius:10px;padding:3px;">
            <button class="search-tab active" id="searchModeAddr" style="flex:1;padding:6px 0;font-size:11px;text-align:center;border:none;border-radius:8px;cursor:pointer;font-weight:600;transition:all 0.2s;">주소 검색</button>
            <button class="search-tab" id="searchModeZone" style="flex:1;padding:6px 0;font-size:11px;text-align:center;border:none;border-radius:8px;cursor:pointer;font-weight:600;transition:all 0.2s;">존 검색</button>
        </div>
        <div style="position:relative;">
            <input type="text" id="regionSearch" placeholder="주소, 장소명 검색" style="width:100%;padding:9px 12px;border-radius:10px;border:1px solid #e8eaed;background:#f7f8fa;color:#1a1a1a;font-size:12px;box-sizing:border-box;transition:all 0.2s;font-weight:500;" onfocus="this.style.borderColor='#0064FF';this.style.boxShadow='0 0 0 3px rgba(0,100,255,0.1)'" onblur="this.style.borderColor='#e8eaed';this.style.boxShadow='none'">
            <div id="searchResults" style="display:none;position:absolute;top:100%;left:0;right:0;max-height:200px;overflow-y:auto;background:#ffffff;border:1px solid #e8eaed;border-top:none;border-radius:0 0 10px 10px;z-index:9999;box-shadow:0 4px 12px rgba(0,0,0,0.08);"></div>
        </div>
    </div>
    <div class="sidebar-section">
        <div class="sidebar-section-title">데이터 레이어</div>
        <button class="sidebar-btn" id="toggleAccess"><span class="dot" style="background:#fb8c00"></span>앱 접속</button>
        <button class="sidebar-btn" id="toggleRes"><span class="dot" style="background:#7e57c2"></span>예약 생성</button>
        <button class="sidebar-btn" id="toggleDtod"><span class="dot" style="background:#00bcd4"></span>부름 호출</button>
        <button class="sidebar-btn" id="toggleZones"><span class="dot" style="background:#27ae60"></span>운영 존</button>
        <div id="zoneTypeFilter" style="display:block;padding:4px 10px 8px 28px;">
            <label style="display:flex;align-items:center;gap:8px;font-size:12px;color:#5a6270;cursor:pointer;padding:6px 0;"><input type="checkbox" class="zone-type-chk" value="0" checked style="accent-color:#27ae60;width:16px;height:16px;"> 일반</label>
            <label style="display:flex;align-items:center;gap:8px;font-size:12px;color:#5a6270;cursor:pointer;padding:6px 0;"><input type="checkbox" class="zone-type-chk" value="5" checked style="accent-color:#8e44ad;width:16px;height:16px;"> 부름우선</label>
            <label style="display:flex;align-items:center;gap:8px;font-size:12px;color:#5a6270;cursor:pointer;padding:6px 0;"><input type="checkbox" class="zone-type-chk" value="3" checked style="accent-color:#0064FF;width:16px;height:16px;"> 부름스테이션</label>
        </div>
        <button class="sidebar-btn" id="toggleGcar"><span class="dot" style="background:#ef5350"></span>그린카 존</button>
        <button class="sidebar-btn" id="toggleClosed"><span class="dot" style="background:#616161"></span>폐쇄 존</button>
    </div>
    <div class="sidebar-section">
        <div class="sidebar-section-title">EV</div>
        <button class="sidebar-btn" id="toggleEvZones"><span class="dot" style="background:#1565c0"></span>EV 운영 존</button>
        <button class="sidebar-btn" id="toggleChargedOnly"><span class="dot" style="background:#0d47a1"></span>충전보장형</button>
        <button class="sidebar-btn" id="toggleEvInfra"><span class="dot" style="background:#42a5f5"></span>충전 인프라</button>
        <div id="evInfraFilter" style="display:none;padding:4px 10px 8px 28px;">
            <label style="display:flex;align-items:center;gap:8px;font-size:12px;color:#5a6270;cursor:pointer;padding:4px 0;"><input type="radio" name="evInfraType" value="all" checked style="accent-color:#42a5f5;width:14px;height:14px;"> 전체</label>
            <label style="display:flex;align-items:center;gap:8px;font-size:12px;color:#5a6270;cursor:pointer;padding:4px 0;"><input type="radio" name="evInfraType" value="everon" style="accent-color:#1565c0;width:14px;height:14px;"> 에버온 (일반)</label>
            <label style="display:flex;align-items:center;gap:8px;font-size:12px;color:#5a6270;cursor:pointer;padding:4px 0;"><input type="radio" name="evInfraType" value="chargev" style="accent-color:#43a047;width:14px;height:14px;"> 차지비 (미니EV)</label>
        </div>
    </div>
    <div class="sidebar-section">
        <div class="sidebar-section-title">분석</div>
        <button class="sidebar-btn" id="toggleGap"><span class="dot" style="background:#8e44ad"></span>미진출 지역 분석</button>
        <button class="sidebar-btn" id="toggleReentry"><span class="dot" style="background:#ec407a"></span>재진입 추천구역</button>
        <!-- <button class="sidebar-btn" id="toggleAnalysis"><span class="dot" style="background:#ef5350"></span>수요/공급 비교 분석</button> -->
        <button class="sidebar-btn" id="toggleMarketShare"><span class="dot" style="background:#ff7043"></span>Market Share</button>
    </div>
    <div class="sidebar-section">
        <div class="sidebar-section-title">업데이트</div>
        <button class="update-btn" id="updateDemandBtn" onclick="runUpdateDemand()">수요 업데이트</button>
        <div class="update-time" id="demandUpdateTime">마지막: {LAST_UPDATE_DEMAND}</div>
        <button class="update-btn" id="updateZoneBtn" onclick="runUpdateZone()">존/실적 업데이트</button>
        <div class="update-time" id="zoneUpdateTime">마지막: {LAST_UPDATE_ZONE}</div>
    </div>
    <div class="legend-row">
        <div class="legend-item"><span class="legend-dot" style="background:#27ae60"></span>일반</div>
        <div class="legend-item"><span class="legend-dot" style="background:#0064FF"></span>스테이션</div>
        <div class="legend-item"><span class="legend-dot" style="background:#8e44ad"></span>부름우선</div>
        <div class="legend-item"><span class="legend-dot" style="background:#ef5350"></span>그린카</div>
        <div class="legend-item"><span class="legend-dot" style="background:#616161"></span>폐쇄</div>
    </div>
</div>

<div class="sim-ctx" id="simCtx"><div class="sim-ctx-item" id="simCtxBtn">존 개설 시뮬레이션</div></div>
<div class="sim-overlay" id="simOverlay"></div>
<div class="sim-panel" id="simPanel">
    <button class="sim-close" id="simClose">&times;</button>
    <h3>존 개설 시뮬레이션</h3>
    <div id="simContent"></div>
</div>

<div class="gap-panel" id="gapPanel">
    <h3>미진출 지역 분석</h3>
    <div style="font-size:11px;color:#8b95a5;margin-bottom:8px;font-weight:500;">반경 500m 수요 집계 기준 | 반경 {'300m' if team_id == 'seoul' else '500m' if team_id == 'gyeonggi' else '800m'} 내 운영 존 없음</div>
    <div id="gapRegionFilter" style="margin-bottom:10px;display:flex;align-items:center;gap:6px;{'display:none;' if len(TEAM_CONFIG[team_id]['regions']) <= 1 else ''}">
        <span style="font-size:11px;color:#8b95a5;font-weight:600;">지역</span>
        <select id="gapRegionSelect" style="padding:6px 12px;border-radius:8px;border:1px solid #e8eaed;background:#fff;color:#1a1a1a;font-size:12px;font-weight:600;">
            <option value="">전체 지역</option>
            {''.join(f'<option value="{r}">{r}</option>' for r in TEAM_CONFIG[team_id]['regions'])}
        </select>
    </div>
    <div id="gapList"></div>
</div>

<div class="gap-panel" id="reentryPanel" style="width:400px;">
    <h3>재진입 추천구역</h3>
    <div style="font-size:11px;color:#8b95a5;margin-bottom:12px;font-weight:500;">
        <div style="display:flex;flex-wrap:wrap;gap:4px 8px;margin-top:4px;">
            <span style="background:#f4f5f7;padding:3px 8px;border-radius:6px;font-size:10px;font-weight:600;">운영대수 &ge;1</span>
            <span style="background:#f4f5f7;padding:3px 8px;border-radius:6px;font-size:10px;font-weight:600;">운영 30일+</span>
            <span style="background:#f4f5f7;padding:3px 8px;border-radius:6px;font-size:10px;font-weight:600;">매출 &ge;160만/28일</span>
            <span style="background:#f4f5f7;padding:3px 8px;border-radius:6px;font-size:10px;font-weight:600;">500m 접속 &ge;100/월</span>
            <span style="background:#f4f5f7;padding:3px 8px;border-radius:6px;font-size:10px;font-weight:600;">300m 내 운영존 없음</span>
        </div>
    </div>
    <div id="reentryRegionFilter" style="margin-bottom:10px;display:flex;align-items:center;gap:6px;{'display:none;' if len(TEAM_CONFIG[team_id]['regions']) <= 1 else ''}">
        <span style="font-size:11px;color:#8b95a5;font-weight:600;">지역</span>
        <select id="reentryRegionSelect" style="padding:6px 12px;border-radius:8px;border:1px solid #e8eaed;background:#fff;color:#1a1a1a;font-size:12px;font-weight:600;">
            <option value="">전체 지역</option>
            {''.join(f'<option value="{r}">{r}</option>' for r in TEAM_CONFIG[team_id]['regions'])}
        </select>
    </div>
    <div style="font-size:10px;color:#8b95a5;margin-bottom:6px;display:flex;justify-content:space-between;"><span>존 이름</span><span>주변수요 | 운영대수 | 운영일</span></div>
    <div id="reentryList"></div>
</div>

<div class="gap-panel" id="analysisPanel" style="width:820px;">
    <h3>수요/공급 비교 분석</h3>
    <div style="display:flex;gap:4px;margin-bottom:10px;align-items:center;">
        <button id="tabGrowth" class="analysis-tab active" onclick="switchAnalysisTab('growth')">수요 성장</button>
        <button id="tabDecline" class="analysis-tab" onclick="switchAnalysisTab('decline')">수요 감소</button>
        {'<div style="margin-left:auto;display:flex;align-items:center;gap:6px;"><span style="font-size:11px;color:#8b95a5;font-weight:600;">지역</span><select id="analysisRegionFilter" style="padding:6px 12px;border-radius:8px;border:1px solid #e8eaed;background:#fff;color:#1a1a1a;font-size:12px;font-weight:600;" onchange="renderAnalysis()"><option value="">전체 지역</option>' + ''.join(f'<option value="{r}">{r}</option>' for r in TEAM_CONFIG[team_id]['regions']) + '</select></div>' if len(TEAM_CONFIG[team_id]['regions']) > 1 else ''}
    </div>
    <div style="font-size:11px;color:#8b95a5;margin-bottom:10px;font-weight:500;" id="analysisDesc">예약 점유율 상승 추세 지역 | 증감·그래프 = {team_name} 전체 대비 점유율 변화 ({team_name} 전체 행은 절대값)</div>
    <table style="width:100%;border-collapse:collapse;font-size:11px;">
        <thead><tr>
            <th data-col="region2" style="text-align:left;padding:6px 6px;border-bottom:2px solid #e0e2e6;cursor:pointer;color:#8b95a5;font-weight:600;">지역</th>
            <th data-col="access_weekly" style="text-align:right;padding:6px 6px;border-bottom:2px solid #e0e2e6;cursor:pointer;color:#8b95a5;font-weight:600;">접속/주</th>
            <th style="padding:6px 6px;border-bottom:2px solid #e0e2e6;color:#8b95a5;font-weight:600;">추이</th>
            <th data-col="access_growth" style="text-align:right;padding:6px 6px;border-bottom:2px solid #e0e2e6;cursor:pointer;color:#8b95a5;font-weight:600;">증감</th>
            <th data-col="res_weekly" style="text-align:right;padding:6px 6px;border-bottom:2px solid #e0e2e6;cursor:pointer;color:#8b95a5;font-weight:600;">예약/주</th>
            <th style="padding:6px 6px;border-bottom:2px solid #e0e2e6;color:#8b95a5;font-weight:600;">추이</th>
            <th data-col="res_growth" style="text-align:right;padding:6px 6px;border-bottom:2px solid #e0e2e6;cursor:pointer;color:#8b95a5;font-weight:600;">증감</th>
            <th data-col="cars" style="text-align:right;padding:6px 6px;border-bottom:2px solid #e0e2e6;cursor:pointer;color:#8b95a5;font-weight:600;">차량</th>
            <th style="padding:6px 6px;border-bottom:2px solid #e0e2e6;color:#8b95a5;font-weight:600;">추이</th>
            <th data-col="car_growth" style="text-align:right;padding:6px 6px;border-bottom:2px solid #e0e2e6;cursor:pointer;color:#8b95a5;font-weight:600;">증감</th>
            <th data-col="status" style="text-align:center;padding:6px 6px;border-bottom:2px solid #e0e2e6;cursor:pointer;color:#8b95a5;font-weight:600;">판정</th>
        </tr></thead>
        <tbody id="analysisBody"></tbody>
    </table>
</div>

<div class="gap-panel" id="marketSharePanel" style="width:680px;">
    <div style="margin-bottom:16px;">
        <h3 style="margin:0 0 6px 0;font-size:16px;">Market Share</h3>
        <div style="font-size:11px;color:#8b95a5;font-weight:500;">지역별 차량수 기준 · 쏘카 vs 그린카 점유율 비교</div>
    </div>
    <table style="width:100%;border-collapse:separate;border-spacing:0;font-size:12px;">
        <thead><tr style="background:#f7f8fa;">
            <th data-ms="region2" style="text-align:left;padding:10px 12px;border-bottom:2px solid #e0e2e6;cursor:pointer;color:#5a6270;font-weight:700;border-radius:8px 0 0 0;">지역</th>
            <th data-ms="socar_cars" style="text-align:right;padding:10px 12px;border-bottom:2px solid #e0e2e6;cursor:pointer;color:#0064FF;font-weight:700;">쏘카 차량</th>
            <th data-ms="socar_zones" style="text-align:right;padding:10px 12px;border-bottom:2px solid #e0e2e6;cursor:pointer;color:#0064FF;font-weight:700;">쏘카 존</th>
            <th data-ms="gcar_cars" style="text-align:right;padding:10px 12px;border-bottom:2px solid #e0e2e6;cursor:pointer;color:#ef5350;font-weight:700;">그린카 차량</th>
            <th data-ms="gcar_zones" style="text-align:right;padding:10px 12px;border-bottom:2px solid #e0e2e6;cursor:pointer;color:#ef5350;font-weight:700;">그린카 존</th>
            <th data-ms="socar_share" style="text-align:right;padding:10px 12px;border-bottom:2px solid #e0e2e6;cursor:pointer;color:#5a6270;font-weight:700;border-radius:0 8px 0 0;">점유율</th>
        </tr></thead>
        <tbody id="marketShareBody"></tbody>
    </table>
</div>


<div class="gap-panel" id="timelinePanel" style="width:720px;max-width:90vw;">
    <div style="display:flex;justify-content:space-between;align-items:center;">
        <h3 id="timelineTitle">예약 현황</h3>
        <span id="timelineClose" style="cursor:pointer;font-size:16px;color:#8b95a5;background:#f4f5f7;width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;transition:all 0.2s;">&times;</span>
    </div>
    <div id="timelineContent" style="overflow-x:auto;"></div>
</div>

<div id="map"></div>
<div style="position:fixed;top:12px;left:272px;z-index:800;background:#fff;padding:7px 14px;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,0.08);font-size:11px;color:#5a6270;font-weight:500;display:flex;align-items:center;gap:6px;pointer-events:none;">
    <span style="font-size:14px;">🖱️</span> 우클릭 시 해당 좌표에서 존 개설 시뮬레이션 가능
</div>
<div id="toolPanel" style="position:fixed;top:12px;right:12px;z-index:800;display:flex;gap:6px;">
    <button id="toolDistance" style="padding:7px 14px;border:none;border-radius:10px;background:#fff;color:#5a6270;font-size:11px;font-weight:600;cursor:pointer;display:flex;align-items:center;gap:4px;transition:all .15s;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <span style="font-size:14px;">📏</span> 거리 재기
    </button>
    <button id="toolRadius" style="padding:7px 14px;border:none;border-radius:10px;background:#fff;color:#5a6270;font-size:11px;font-weight:600;cursor:pointer;display:flex;align-items:center;gap:4px;transition:all .15s;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <span style="font-size:14px;">⭕</span> 반경 측정
    </button>
</div>
<div id="measureGuide" style="display:none;position:fixed;top:50px;right:12px;z-index:1000;background:#fff;border-radius:10px;padding:10px 14px;font-size:10px;color:#5a6270;max-width:180px;box-shadow:0 4px 16px rgba(0,0,0,0.1);font-weight:500;"></div>

<script>
var accessData = {jd(access_heat)};
var resData = {jd(res_heat)};
var dtodData = {jd(dtod_dots)};
var zonesData = {jd(zones_data)};
var gapsData = {jd(gaps)};
var reentryData = {jd(reentry_data or [])};
var evData = {jd(ev_data or [])};
var analysisGrowth = {jd(analysis_growth)};
var analysisDecline = {jd(analysis_decline)};
var gcarData = {jd(gcar_zones)};
var closedData = {jd(closed_zones)};
var marketShareData = {jd(market_share)};
var teamTotalLabel = {jd(team_name + ' 전체')};
var timelineData = {jd(timeline_data)};
var regions = {jd(regions)};
var lastUpdateDemand = '{LAST_UPDATE_DEMAND}';
var lastUpdateZone = '{LAST_UPDATE_ZONE}';
document.getElementById('demandUpdateTime').textContent = '마지막: ' + lastUpdateDemand;
document.getElementById('zoneUpdateTime').textContent = '마지막: ' + lastUpdateZone;
// 로컬 서버 실행 시 최신 값으로 갱신
fetch('/api/status').then(function(r){{return r.json();}}).then(function(d){{
    lastUpdateDemand = d.last_update_demand || lastUpdateDemand;
    lastUpdateZone = d.last_update_zone || lastUpdateZone;
    document.getElementById('demandUpdateTime').textContent = '마지막: ' + lastUpdateDemand;
    document.getElementById('zoneUpdateTime').textContent = '마지막: ' + lastUpdateZone;
}}).catch(function(){{}});

// 검색 기능
var searchInput = document.getElementById('regionSearch');
var searchResults = document.getElementById('searchResults');
var searchItemStyle = 'padding:6px 10px;cursor:pointer;font-size:11px;color:#c0c8e0;border-bottom:1px solid #2a2f45;';

var searchMode = 'addr';
var modeZoneBtn = document.getElementById('searchModeZone');
var modeAddrBtn = document.getElementById('searchModeAddr');
modeZoneBtn.addEventListener('click', function() {{
    searchMode = 'zone';
    styleBtn(modeZoneBtn, true); styleBtn(modeAddrBtn, false);
    searchInput.placeholder = '존 이름, 지역명 검색';
    searchInput.value = ''; searchResults.style.display = 'none';
    searchInput.focus();
}});
modeAddrBtn.addEventListener('click', function() {{
    searchMode = 'addr';
    styleBtn(modeAddrBtn, true); styleBtn(modeZoneBtn, false);
    searchInput.placeholder = '주소, 장소명 검색 (카카오맵)';
    searchInput.value = ''; searchResults.style.display = 'none';
    searchInput.focus();
}});

function doZoneSearch(query) {{
    searchResults.innerHTML = '';
    if (!query || query.length < 1) {{ searchResults.style.display = 'none'; return; }}
    var q = query.toLowerCase();
    var matches = [];
    regions.forEach(function(r) {{
        if (r.replace(/\\u3000/g, ' ').toLowerCase().indexOf(q) >= 0) {{
            matches.push({{ type: 'region', label: r.replace(/\\u3000/g, ' '), value: r }});
        }}
    }});
    zonesData.forEach(function(z) {{
        if (z.zone_name.toLowerCase().indexOf(q) >= 0 || z.parking_name.toLowerCase().indexOf(q) >= 0 || (z.address && z.address.toLowerCase().indexOf(q) >= 0) || String(z.zone_id) === q) {{
            matches.push({{ type: 'zone', label: z.zone_name + ' (' + z.region2.replace(/\\u3000/g, ' ') + ')', value: z }});
        }}
    }});
    closedData.forEach(function(cz) {{
        if (cz.zone_name.toLowerCase().indexOf(q) >= 0 || cz.parking_name.toLowerCase().indexOf(q) >= 0 || (cz.address && cz.address.toLowerCase().indexOf(q) >= 0) || String(cz.zone_id) === q) {{
            matches.push({{ type: 'closed', label: cz.zone_name + ' (' + cz.region2.replace(/\\u3000/g, ' ') + ')', value: cz }});
        }}
    }});
    if (matches.length === 0) {{
        searchResults.innerHTML = '<div style="' + searchItemStyle + 'color:#6b7394;">결과 없음</div>';
        searchResults.style.display = 'block';
        return;
    }}
    matches.slice(0, 20).forEach(function(m) {{
        var div = document.createElement('div');
        div.style.cssText = searchItemStyle;
        var icon = m.type === 'region' ? '📍 ' : m.type === 'closed' ? '⛔ ' : '🅿️ ';
        div.textContent = icon + m.label;
        div.addEventListener('mouseenter', function() {{ this.style.background = '#3a4060'; }});
        div.addEventListener('mouseleave', function() {{ this.style.background = 'transparent'; }});
        div.addEventListener('click', function() {{
            searchResults.style.display = 'none';
            if (m.type === 'region') {{
                filterByRegion(m.value);
                searchInput.value = m.label;
            }} else if (m.type === 'closed') {{
                searchInput.value = m.label;
                if (!showClosed) {{ showClosed = true; closedLayer.addTo(map); styleBtn(document.getElementById('toggleClosed'), true); }}
                map.setView([m.value.lat, m.value.lng], 16);
                closedLayer.eachLayer(function(lyr) {{
                    if (lyr.getLatLng && Math.abs(lyr.getLatLng().lat - m.value.lat) < 0.0001 && Math.abs(lyr.getLatLng().lng - m.value.lng) < 0.0001) lyr.openPopup();
                }});
            }} else {{
                filterByRegion('');
                searchInput.value = m.label;
                map.setView([m.value.lat, m.value.lng], 16);
                allZoneMarkers.forEach(function(mk) {{
                    if (mk._zoneData.zone_id === m.value.zone_id) mk.openPopup();
                }});
            }}
        }});
        searchResults.appendChild(div);
    }});
    searchResults.style.display = 'block';
}}

var kakaoTimer = null;
var addrPin = null;
function placeAddrPin(lat, lng, name, addr) {{
    if (addrPin) map.removeLayer(addrPin);
    addrPin = L.marker([lat, lng], {{
        icon: L.divIcon({{
            className: 'zone-marker-wrap',
            html: '<div style="font-size:36px;line-height:1;filter:drop-shadow(0 2px 4px rgba(0,0,0,0.5));">📍</div>',
            iconSize: [36, 36], iconAnchor: [18, 36], popupAnchor: [0, -32]
        }})
    }}).addTo(map).bindPopup('<b>' + name + '</b><br><span style="font-size:11px;color:#888;">' + addr + '</span>').openPopup();
    map.setView([lat, lng], 16);
}}
function geocodeAndPin(query) {{
    fetch('https://dapi.kakao.com/v2/local/search/address.json?query=' + encodeURIComponent(query) + '&size=1', {{
        headers: {{ 'Authorization': 'KakaoAK 9a5596ace397f435fc33097cb2dc75e8' }}
    }})
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
        if (data.documents && data.documents.length > 0) {{
            var doc = data.documents[0];
            placeAddrPin(parseFloat(doc.y), parseFloat(doc.x), doc.address_name, doc.address ? doc.address.region_3depth_name : '');
            searchResults.style.display = 'none';
            return;
        }}
        // 주소 매칭 실패 시 키워드 검색 fallback
        fetch('https://dapi.kakao.com/v2/local/search/keyword.json?query=' + encodeURIComponent(query) + '&size=1', {{
            headers: {{ 'Authorization': 'KakaoAK 9a5596ace397f435fc33097cb2dc75e8' }}
        }})
        .then(function(r) {{ return r.json(); }})
        .then(function(data2) {{
            if (data2.documents && data2.documents.length > 0) {{
                var doc = data2.documents[0];
                placeAddrPin(parseFloat(doc.y), parseFloat(doc.x), doc.place_name, doc.address_name);
            }} else {{
                searchResults.innerHTML = '<div style="' + searchItemStyle + 'color:#6b7394;">주소를 찾을 수 없습니다</div>';
                searchResults.style.display = 'block';
            }}
        }});
    }})
    .catch(function() {{}});
}}
function doAddrSearch(query) {{
    clearTimeout(kakaoTimer);
    searchResults.innerHTML = '';
    if (!query || query.length < 2) {{ searchResults.style.display = 'none'; return; }}
    searchResults.innerHTML = '<div style="' + searchItemStyle + 'color:#6b7394;">검색 중...</div>';
    searchResults.style.display = 'block';
    kakaoTimer = setTimeout(function() {{
        fetch('https://dapi.kakao.com/v2/local/search/keyword.json?query=' + encodeURIComponent(query) + '&size=10', {{
            headers: {{ 'Authorization': 'KakaoAK 9a5596ace397f435fc33097cb2dc75e8' }}
        }})
        .then(function(r) {{ return r.json(); }})
        .then(function(data) {{
            searchResults.innerHTML = '';
            if (!data.documents || data.documents.length === 0) {{
                searchResults.innerHTML = '<div style="' + searchItemStyle + 'color:#6b7394;">결과 없음</div>';
                return;
            }}
            data.documents.forEach(function(doc) {{
                var div = document.createElement('div');
                div.style.cssText = searchItemStyle;
                div.innerHTML = '🔍 <b>' + doc.place_name + '</b> <span style="color:#6b7394;font-size:10px;">' + doc.address_name + '</span>';
                div.addEventListener('mouseenter', function() {{ this.style.background = '#3a4060'; }});
                div.addEventListener('mouseleave', function() {{ this.style.background = 'transparent'; }});
                div.addEventListener('click', function() {{
                    searchResults.style.display = 'none';
                    searchInput.value = doc.place_name;
                    var lat = parseFloat(doc.y), lng = parseFloat(doc.x);
                    placeAddrPin(lat, lng, doc.place_name, doc.address_name);
                }});
                searchResults.appendChild(div);
            }});
        }})
        .catch(function() {{
            searchResults.innerHTML = '<div style="' + searchItemStyle + 'color:#e74c3c;">API 오류 (서비스 활성화 확인)</div>';
        }});
    }}, 300);
}}

searchInput.addEventListener('input', function() {{
    if (searchMode === 'zone') doZoneSearch(this.value);
    else doAddrSearch(this.value);
}});
searchInput.addEventListener('focus', function() {{
    if (this.value) {{
        if (searchMode === 'zone') doZoneSearch(this.value);
        else doAddrSearch(this.value);
    }}
}});
document.addEventListener('click', function(e) {{
    if (!searchInput.contains(e.target) && !searchResults.contains(e.target)) searchResults.style.display = 'none';
}});
// 전체 보기 복원: 입력 비우면 리셋
searchInput.addEventListener('keydown', function(e) {{
    if (e.key === 'Escape') {{ this.value = ''; searchResults.style.display = 'none'; filterByRegion(''); }}
    if (e.key === 'Enter' && searchMode === 'addr' && this.value.trim().length >= 2) {{
        e.preventDefault();
        geocodeAndPin(this.value.trim());
    }}
}});

function filterByRegion(region) {{
    zoneLayer.clearLayers();
    allZoneMarkers.forEach(function(m) {{
        if (!region || m._zoneData.region2 === region) zoneLayer.addLayer(m);
    }});
    if (region) {{
        var rz = zonesData.filter(function(z) {{ return z.region2 === region; }});
        if (rz.length > 0) {{
            var lats = rz.map(function(z) {{ return z.lat; }});
            var lngs = rz.map(function(z) {{ return z.lng; }});
            var pad = 0.03;
            map.fitBounds([
                [Math.min.apply(null, lats) - pad, Math.min.apply(null, lngs) - pad],
                [Math.max.apply(null, lats) + pad, Math.max.apply(null, lngs) + pad]
            ]);
        }}
    }} else {{
        map.setView([{team_center[0]}, {team_center[1]}], {team_zoom});
    }}
}}

var TEAM_ID = '{team_id}';
var map = L.map('map', {{ zoomControl: false }}).setView([{team_center[0]}, {team_center[1]}], {team_zoom});
{TILE_SETUP}
{ZONE_JS}

var maxAccessVal = Math.max.apply(null, accessData.map(function(d) {{ return d[2]; }}));
var maxResVal = Math.max.apply(null, resData.map(function(d) {{ return d[2]; }}));

function formatCount(n) {{ if (n >= 10000) return (n/10000).toFixed(1) + '만'; if (n >= 1000) return (n/1000).toFixed(1) + '천'; return n.toString(); }}

// 접속 건수 → 색상 (주황 스펙트럼)
function accessColor(val) {{
    var ratio = Math.min(1, Math.log(val + 1) / Math.log(maxAccessVal + 1));
    var colors = [
        [255,243,224], [255,204,128], [255,167,38],
        [251,140,0], [239,108,0], [230,81,0]
    ];
    var idx = ratio * (colors.length - 1);
    var lo = Math.floor(idx), hi = Math.min(lo + 1, colors.length - 1);
    var t = idx - lo;
    var r = Math.round(colors[lo][0] + (colors[hi][0] - colors[lo][0]) * t);
    var g = Math.round(colors[lo][1] + (colors[hi][1] - colors[lo][1]) * t);
    var b = Math.round(colors[lo][2] + (colors[hi][2] - colors[lo][2]) * t);
    return 'rgb(' + r + ',' + g + ',' + b + ')';
}}

// 예약 건수 → 색상 (파랑 스펙트럼)
function resColor(val) {{
    var ratio = Math.min(1, Math.log(val + 1) / Math.log(maxResVal + 1));
    var colors = [
        [237,231,246], [186,164,220], [149,117,205],
        [126,87,194], [103,58,183], [69,39,160]
    ];
    var idx = ratio * (colors.length - 1);
    var lo = Math.floor(idx), hi = Math.min(lo + 1, colors.length - 1);
    var t = idx - lo;
    var r = Math.round(colors[lo][0] + (colors[hi][0] - colors[lo][0]) * t);
    var g = Math.round(colors[lo][1] + (colors[hi][1] - colors[lo][1]) * t);
    var b = Math.round(colors[lo][2] + (colors[hi][2] - colors[lo][2]) * t);
    return 'rgb(' + r + ',' + g + ',' + b + ')';
}}

function dotRadius(val, maxVal) {{
    return Math.max(4, Math.min(14, 3 + Math.sqrt(val / maxVal) * 11));
}}
function dotOpacity(val, maxVal) {{
    return Math.max(0.82, Math.min(0.97, 0.8 + (val / maxVal) * 0.17));
}}

var accessLayer = L.layerGroup();
accessData.forEach(function(d) {{
    L.circleMarker([d[0], d[1]], {{
        radius: dotRadius(d[2], maxAccessVal),
        fillColor: accessColor(d[2]),
        color: 'rgba(191,54,12,0.7)', weight: 1.5,
        fillOpacity: dotOpacity(d[2], maxAccessVal)
    }}).bindPopup('<div style="font-weight:600">앱 접속 지점</div><div>주평균 접속: <b>' + d[2].toLocaleString() + '</b></div>').addTo(accessLayer);
}});
accessLayer.addTo(map);

var resLayer = L.layerGroup();
resData.forEach(function(d) {{
    L.circleMarker([d[0], d[1]], {{
        radius: dotRadius(d[2], maxResVal),
        fillColor: resColor(d[2]),
        color: 'rgba(69,39,160,0.7)', weight: 1.5,
        fillOpacity: dotOpacity(d[2], maxResVal)
    }}).bindPopup('<div style="font-weight:600">예약 생성 지점</div><div>주평균 예약: <b>' + d[2].toLocaleString() + '</b></div>').addTo(resLayer);
}});
resLayer.addTo(map);


var zoneLayer = L.layerGroup();
var allZoneMarkers = [];
// EV 가상존 ID 수집 (별도 마커 제외용)
var evVirtualZoneIds = {{}};
zonesData.forEach(function(z) {{
    if (z.ev_zone) evVirtualZoneIds[z.ev_zone.zone_id] = true;
}});
zonesData.forEach(function(z) {{
    // 가상존(전기차)은 원본존에 귀속 → 별도 마커 미표시
    if (evVirtualZoneIds[z.zone_id]) return;
    var hasRightCol = z.ev_zone || (z.ev_detail && z.ev_detail.length > 0);
    var popupOpts = hasRightCol ? {{ maxWidth: 560 }} : {{ maxWidth: 300 }};
    var evZoneId = z.ev_zone ? z.ev_zone.zone_id : null;
    var evZoneName = z.ev_zone ? z.ev_zone.zone_name : null;
    var m = L.marker([z.lat, z.lng], {{ icon: makeZoneIcon(z) }}).bindPopup(makePopup(z), popupOpts);
    m._zoneData = z;
    allZoneMarkers.push(m);
    zoneLayer.addLayer(m);
}});
zoneLayer.addTo(map);

// 팝업 닫힐 때 타임라인도 닫기
map.on('popupclose', function() {{
    document.getElementById('timelinePanel').style.display = 'none';
}});
// 타임라인 X 클릭 시 팝업도 닫기
document.getElementById('timelineClose').addEventListener('click', function() {{
    document.getElementById('timelinePanel').style.display = 'none';
    map.closePopup();
}});

var dtodLayer = L.layerGroup();
var maxDtodVal = dtodData.reduce(function(m,d){{ return Math.max(m,d[2]); }}, 1);
dtodData.forEach(function(d) {{
    L.circleMarker([d[0], d[1]], {{
        radius: dotRadius(d[2], maxDtodVal), fillColor: '#ff4081', color: '#fff',
        weight: 1.5, fillOpacity: dotOpacity(d[2], maxDtodVal)
    }}).bindPopup('<div style="font-weight:600">부름 호출 지점</div><div>90일간 호출: <b>' + d[2].toLocaleString() + '건</b></div>').addTo(dtodLayer);
}});

var gapLayer = L.layerGroup();
var gapPopup = function(g) {{
    return '<div class="popup-title">' + (g.name || '(' + g.lat + ', ' + g.lng + ')') + '</div>' +
        '<div style="font-size:10px;color:#8b95a5;margin-bottom:4px;">반경 500m 내 수요 · 월평균 · ' + '{THREE_MONTHS_AGO}~{TODAY}' + '</div>' +
        '<div class="popup-row"><span class="popup-label">접속</span><b style="color:#0064FF">' + (g.access_count || 0).toLocaleString() + '</b></div>' +
        '<div class="popup-row"><span class="popup-label">예약 생성</span><b style="color:#0064FF">' + (g.reservation_count || 0).toLocaleString() + '</b></div>' +
        '<div class="popup-row"><span class="popup-label">최근접 존</span><b>' + (g.nearest_zone_km || '-') + 'km</b></div>';
}};
gapsData.forEach(function(g) {{
    L.circle([g.lat, g.lng], {{
        radius: 500,
        fillColor: '#e74c3c', color: '#c0392b', weight: 1.5, opacity: 0.7, fillOpacity: 0.12
    }}).bindPopup(gapPopup(g)).addTo(gapLayer);
    L.circleMarker([g.lat, g.lng], {{
        radius: 3, fillColor: '#c0392b', color: '#c0392b', weight: 0, fillOpacity: 0.8
    }}).bindPopup(gapPopup(g)).addTo(gapLayer);
}});

var gapListDiv = document.getElementById('gapList');
gapsData.sort(function(a,b) {{ return (b.access_count + b.reservation_count) - (a.access_count + a.reservation_count); }});
function renderGapList(filterRegion) {{
    gapListDiv.innerHTML = '';
    gapsData.forEach(function(g) {{
        if (filterRegion && g.region1 !== filterRegion) return;
        var row = document.createElement('div');
        row.className = 'gap-row';
        row.innerHTML = '<span class="gap-name">' + (g.name || '(' + g.lat.toFixed(3) + ', ' + g.lng.toFixed(3) + ')') + '</span>' +
            '<span class="gap-cnt" style="font-size:10px">접속 ' + (g.access_count||0).toLocaleString() + '/월 | 예약 ' + (g.reservation_count||0).toLocaleString() + '/월</span>';
        row.style.cursor = 'pointer';
        row.addEventListener('click', function() {{ map.setView([g.lat, g.lng], 15); }});
        gapListDiv.appendChild(row);
    }});
}}
renderGapList('');
var gapRegionSelect = document.getElementById('gapRegionSelect');
if (gapRegionSelect) {{
    gapRegionSelect.addEventListener('change', function() {{
        renderGapList(this.value);
    }});
}}

// 재진입 추천구역 레이어
var reentryLayer = L.layerGroup();
var reentryPopup = function(z) {{
    return '<div class="popup-title">' + z.zone_name + ' <span class="popup-badge" style="background:#ec407a">재진입 추천</span></div>' +
        '<div class="popup-row"><span class="popup-label">주차장</span><span>' + z.parking_name + '</span></div>' +
        '<div class="popup-row"><span class="popup-label">주소</span><span>' + z.address + '</span></div>' +
        (z.provider_name ? '<div class="popup-row"><span class="popup-label">거래처</span><span><b>' + z.provider_name + '</b></span></div>' : '') +
        (z.settlement_type ? '<div class="popup-row"><span class="popup-label">정산방식</span><span>' + z.settlement_type + '</span></div>' : '') +
        (z.price_per_car ? '<div class="popup-row"><span class="popup-label">대당 주차비</span><span><b>' + (z.price_per_car).toLocaleString() + '</b>원/월</span></div>' : '') +
        '<div style="border-top:1px solid #f0f1f3;margin:6px 0;"></div>' +
        '<div style="font-size:10px;color:#8b95a5;font-weight:700;margin-bottom:4px;">반경 500m 내 수요 · 월평균 · {THREE_MONTHS_AGO}~{TODAY}</div>' +
        '<div class="popup-row"><span class="popup-label">접속</span><span><b style="color:#0064FF">' + (z.nearby_access||0).toLocaleString() + '</b></span></div>' +
        '<div class="popup-row"><span class="popup-label">예약 생성</span><span><b style="color:#0064FF">' + (z.nearby_res||0).toLocaleString() + '</b></span></div>' +
        '<div style="border-top:1px solid #f0f1f3;margin:6px 0;"></div>' +
        '<div style="font-size:10px;color:#8b95a5;font-weight:700;margin-bottom:4px;">과거 운영 실적</div>' +
        '<div class="popup-row"><span class="popup-label">운영대수</span><span><b>' + z.hist_car_count + '</b>대</span></div>' +
        '<div class="popup-row"><span class="popup-label">운영기간</span><span>' + z.first_date + ' ~ ' + z.last_date + '</span></div>' +
        '<div class="popup-row"><span class="popup-label">운영일수</span><span><b>' + z.operation_days + '</b>일</span></div>' +
        '<div class="popup-row"><span class="popup-label">가동률</span><span><b>' + z.utilization_rate + '</b>%</span></div>' +
        '<div class="popup-row"><span class="popup-label">대당 매출</span><span><b style="color:#0064FF">' + (z.revenue_per_car_28d||0).toLocaleString() + '</b>원/28일</span></div>' +
        '<div class="popup-row"><span class="popup-label">대당 GP</span><span><b style="color:' + (z.gp_per_car_28d < 0 ? '#e53935' : 'inherit') + '">' + (z.gp_per_car_28d||0).toLocaleString() + '</b>원/28일</span></div>';
}};
var reentryMarkerMap = {{}};
reentryData.forEach(function(z) {{
    L.circle([z.lat, z.lng], {{
        radius: 500,
        fillColor: '#ec407a', color: '#c2185b', weight: 1.5, opacity: 0.7, fillOpacity: 0.12
    }}).bindPopup(reentryPopup(z)).addTo(reentryLayer);
    var dot = L.circleMarker([z.lat, z.lng], {{
        radius: 3, fillColor: '#c2185b', color: '#c2185b', weight: 0, fillOpacity: 0.8
    }}).bindPopup(reentryPopup(z)).addTo(reentryLayer);
    reentryMarkerMap[z.zone_id] = dot;
}});

var reentryListDiv = document.getElementById('reentryList');
function renderReentryList(filterRegion) {{
    reentryListDiv.innerHTML = '';
    var filtered = reentryData.filter(function(z) {{ return !filterRegion || z.region1 === filterRegion; }});
    if (filtered.length === 0) {{
        reentryListDiv.innerHTML = '<div style="text-align:center;color:#8b95a5;padding:20px;font-size:12px;">조건에 맞는 추천구역이 없습니다</div>';
        return;
    }}
    filtered.forEach(function(z) {{
        var row = document.createElement('div');
        row.className = 'gap-row';
        var demand = (z.nearby_access||0) + (z.nearby_res||0);
        row.innerHTML = '<span class="gap-name" style="font-size:12px;">' + z.zone_name + '</span>' +
            '<span class="gap-cnt" style="font-size:10px">' + demand.toLocaleString() + '/월 | ' + z.hist_car_count + '대 | ' + z.operation_days + '일</span>';
        row.style.cursor = 'pointer';
        row.addEventListener('click', function() {{
            map.setView([z.lat, z.lng], 16);
            var m = reentryMarkerMap[z.zone_id];
            if (m) setTimeout(function() {{ m.openPopup(); }}, 300);
        }});
        reentryListDiv.appendChild(row);
    }});
}}
renderReentryList('');
var reentryRegionSelect = document.getElementById('reentryRegionSelect');
if (reentryRegionSelect) {{
    reentryRegionSelect.addEventListener('change', function() {{
        renderReentryList(this.value);
    }});
}}

// 충전 인프라 레이어 (운영존 미매칭 충전소만 = 신규 EV존 후보)
var evLayer = L.layerGroup();
var evAllMarkers = [];
evData.forEach(function(e) {{
    var nearZone = zonesData.some(function(z) {{
        var dlat = (z.lat - e.lat) * 111; var dlng = (z.lng - e.lng) * 111 * Math.cos(e.lat * Math.PI / 180);
        return Math.sqrt(dlat*dlat + dlng*dlng) <= 0.1;
    }});
    if (nearZone) return;
    if (e.total < 3) return;
    if (!e.everon && !e.chargev) return;
    var label = (e.fast > 0 ? '급속' + e.fast : '') + (e.fast > 0 && e.slow > 0 ? '+' : '') + (e.slow > 0 ? '완속' + e.slow : '');
    var roamingTag = (e.everon && e.chargev) ? '에버온+차지비' : e.everon ? '에버온' : '차지비';
    var m = L.circleMarker([e.lat, e.lng], {{
        radius: Math.max(5, Math.min(12, 4 + e.total)),
        fillColor: '#1565c0', color: '#0d47a1', weight: 1.5, opacity: 0.8, fillOpacity: 0.4
    }}).bindPopup(
        '<div class="popup-title">⚡ ' + e.statNm + '</div>' +
        '<div class="popup-row"><span class="popup-label">주소</span><span>' + e.addr + '</span></div>' +
        '<div class="popup-row"><span class="popup-label">운영기관</span><span>' + e.busiNm + '</span></div>' +
        '<div class="popup-row"><span class="popup-label">로밍</span><span style="font-weight:600;color:#1565c0">' + roamingTag + '</span></div>' +
        '<div class="popup-row"><span class="popup-label">충전기</span><span><b style="color:#1565c0">' + e.total + '기</b> (' + label + ')</span></div>' +
        '<div class="popup-row"><span class="popup-label">이용시간</span><span>' + (e.useTime || '정보없음') + '</span></div>' +
        (e.congestion ? evCongestionChart(e.congestion) : '') +
        '<div style="margin-top:6px;padding:4px 8px;background:#e3f2fd;border-radius:6px;font-size:10px;color:#0d47a1;font-weight:600;">쏘카 미운영 · EV존 개설 후보</div>'
    );
    evAllMarkers.push({{ marker: m, everon: e.everon, chargev: e.chargev }});
}});

function rebuildEvLayer() {{
    evLayer.clearLayers();
    var filter = document.querySelector('input[name="evInfraType"]:checked');
    var mode = filter ? filter.value : 'everon';
    evAllMarkers.forEach(function(item) {{
        if (mode === 'all') evLayer.addLayer(item.marker);
        else if (mode === 'everon' && item.everon) evLayer.addLayer(item.marker);
        else if (mode === 'chargev' && item.chargev) evLayer.addLayer(item.marker);
    }});
}}
rebuildEvLayer();

// 그린카 존 레이어
var gcarLayer = L.layerGroup();
gcarData.forEach(function(g) {{
    var size = Math.max(18, Math.min(32, 16 + g.total_cars * 1.5));
    var html = '<div class="zone-pin" style="width:' + size + 'px;height:' + size + 'px;background:rgba(239,83,80,0.75);border-color:rgba(255,180,180,0.8);font-size:9px;">' + g.total_cars + '</div>';
    var icon = L.divIcon({{
        className: 'zone-marker-wrap',
        html: html,
        iconSize: [size, size],
        iconAnchor: [size/2, size/2],
        popupAnchor: [0, -size/2]
    }});
    L.marker([g.lat, g.lng], {{ icon: icon }}).bindPopup(
        '<div class="popup-title" style="color:#ef5350">' + g.zone_name + '</div>' +
        '<div class="popup-row"><span class="popup-label">존 ID</span><span style="color:#666;font-family:monospace">' + g.zone_id + '</span></div>' +
        '<div class="popup-row"><span class="popup-label">지역</span><span>' + g.sig_name + '</span></div>' +
        '<div class="popup-row"><span class="popup-label">차량</span><b>' + g.total_cars + '대</b></div>' +
        '<div style="margin-top:4px;font-size:10px;color:#ef5350;font-weight:600;">그린카</div>'
    ).addTo(gcarLayer);
}});

// 폐쇄 존 레이어
var closedLayer = L.layerGroup();
closedData.forEach(function(cz) {{
    var size = 16;
    var html = '<div class="zone-pin" style="width:' + size + 'px;height:' + size + 'px;background:rgba(97,97,97,0.75);border-color:rgba(180,180,180,0.8);"></div>';
    var icon = L.divIcon({{
        className: 'zone-marker-wrap',
        html: html,
        iconSize: [size, size],
        iconAnchor: [size/2, size/2],
        popupAnchor: [0, -size/2]
    }});
    var perfHtml = '';
    if (cz.revenue_per_car_28d > 0) {{
        perfHtml = '<div class="popup-row"><span class="popup-label">대당매출(4주)</span><b style="color:#ffb74d">' + cz.revenue_per_car_28d.toLocaleString() + '원</b></div>' +
            '<div class="popup-row"><span class="popup-label">대당GP(4주)</span><b style="color:' + (cz.gp_per_car_28d < 0 ? '#e53935' : 'inherit') + '">' + cz.gp_per_car_28d.toLocaleString() + '원</b></div>' +
            '<div class="popup-row"><span class="popup-label">가동률</span><span>' + (cz.utilization_rate > 0 ? cz.utilization_rate.toFixed(1) + '%' : '-') + '</span></div>';
    }} else {{
        perfHtml = '<div class="popup-row"><span class="popup-label">실적</span><span style="color:#888">데이터 없음</span></div>';
    }}
    L.marker([cz.lat, cz.lng], {{ icon: icon }}).bindPopup(
        '<div class="popup-title" style="color:#616161">' + cz.zone_name + '</div>' +
        '<div class="popup-row"><span class="popup-label">존 ID</span><span style="color:#666;font-family:monospace">' + cz.zone_id + '</span></div>' +
        '<div class="popup-row"><span class="popup-label">주차장</span><span>' + cz.parking_name + '</span></div>' +
        '<div class="popup-row"><span class="popup-label">주소</span><span>' + cz.address + '</span></div>' +
        (cz.first_date ? '<div class="popup-row"><span class="popup-label">운영기간</span><span>' + cz.first_date + ' ~ ' + cz.last_date + ' (' + cz.operation_days + '일)</span></div>' : '') +
        '<div class="popup-row"><span class="popup-label">운영시 차량</span><b>' + (cz.hist_car_count > 0 ? cz.hist_car_count.toFixed(1) : '0') + '대</b></div>' +
        perfHtml +
        (cz.provider_name ? '<div class="popup-row"><span class="popup-label">사업자</span><span>' + cz.provider_name + '</span></div>' : '') +
        (cz.settlement_type ? '<div class="popup-row"><span class="popup-label">정산</span><span>' + cz.settlement_type + '</span></div>' : '') +
        (cz.price_per_car > 0 ? '<div class="popup-row"><span class="popup-label">주차비(대당/월)</span><span>' + cz.price_per_car.toLocaleString() + '원</span></div>' : '') +
        '<div style="margin-top:4px;font-size:10px;color:#616161;font-weight:600;">폐쇄 존</div>'
    ).addTo(closedLayer);
}});

// Market Share 테이블
var msSortCol = 'socar_share', msSortAsc = true;
function renderMarketShare() {{
    var sorted = marketShareData.slice().sort(function(a, b) {{
        if (msSortCol === 'region2') return msSortAsc ? a.region2.localeCompare(b.region2) : b.region2.localeCompare(a.region2);
        return msSortAsc ? a[msSortCol] - b[msSortCol] : b[msSortCol] - a[msSortCol];
    }});
    var totSocar = 0, totGcar = 0, totSocarZones = 0, totGcarZones = 0;
    marketShareData.forEach(function(d) {{ totSocar += d.socar_cars; totGcar += d.gcar_cars; totSocarZones += d.socar_zones; totGcarZones += d.gcar_zones; }});
    var avgShare = (totSocar + totGcar) > 0 ? (totSocar / (totSocar + totGcar) * 100).toFixed(1) : '0';
    var html = '<tr style="background:#f0f4ff;">' +
        '<td style="padding:10px 12px;border-bottom:2px solid #e0e2e6;font-weight:800;color:#1a1a1a;">' + teamTotalLabel + '</td>' +
        '<td style="text-align:right;padding:10px 12px;border-bottom:2px solid #e0e2e6;font-weight:800;color:#0064FF;">' + totSocar.toLocaleString() + '</td>' +
        '<td style="text-align:right;padding:10px 12px;border-bottom:2px solid #e0e2e6;font-weight:800;color:#0064FF;">' + totSocarZones.toLocaleString() + '</td>' +
        '<td style="text-align:right;padding:10px 12px;border-bottom:2px solid #e0e2e6;font-weight:800;color:#ef5350;">' + totGcar.toLocaleString() + '</td>' +
        '<td style="text-align:right;padding:10px 12px;border-bottom:2px solid #e0e2e6;font-weight:800;color:#ef5350;">' + totGcarZones.toLocaleString() + '</td>' +
        '<td style="text-align:right;padding:10px 12px;border-bottom:2px solid #e0e2e6;font-weight:800;">' + avgShare + '%</td></tr>';
    sorted.forEach(function(d, idx) {{
        var shareColor = d.socar_share < 40 ? '#c62828' : d.socar_share < 50 ? '#e65100' : d.socar_share >= 70 ? '#18a34a' : '#1a1a1a';
        var barW = d.socar_share.toFixed(0);
        var rowBg = idx % 2 === 0 ? '' : 'background:#fafbfc;';
        html += '<tr class="ms-row" style="' + rowBg + '">' +
            '<td style="padding:8px 12px;border-bottom:1px solid #f0f1f3;font-weight:600;color:#1a1a1a;">' + d.region2.replace(/\\u3000/g,' ') + '</td>' +
            '<td style="text-align:right;padding:8px 12px;border-bottom:1px solid #f0f1f3;font-weight:600;color:#0064FF;">' + d.socar_cars.toLocaleString() + '</td>' +
            '<td style="text-align:right;padding:8px 12px;border-bottom:1px solid #f0f1f3;color:#8b95a5;">' + d.socar_zones + '</td>' +
            '<td style="text-align:right;padding:8px 12px;border-bottom:1px solid #f0f1f3;font-weight:600;color:#ef5350;">' + d.gcar_cars.toLocaleString() + '</td>' +
            '<td style="text-align:right;padding:8px 12px;border-bottom:1px solid #f0f1f3;color:#8b95a5;">' + d.gcar_zones + '</td>' +
            '<td style="text-align:right;padding:8px 12px;border-bottom:1px solid #f0f1f3;position:relative;font-weight:700;color:' + shareColor + ';">' +
                '<div style="position:absolute;top:3px;bottom:3px;left:0;width:' + barW + '%;background:linear-gradient(90deg,rgba(0,100,255,0.06),rgba(0,100,255,0.12));border-radius:4px;"></div>' +
                '<span style="position:relative;">' + d.socar_share.toFixed(1) + '%</span></td></tr>';
    }});
    document.getElementById('marketShareBody').innerHTML = html;
}}
document.querySelectorAll('#marketSharePanel th').forEach(function(th) {{
    th.addEventListener('click', function() {{
        var col = this.dataset.ms;
        if (msSortCol === col) msSortAsc = !msSortAsc;
        else {{ msSortCol = col; msSortAsc = col === 'socar_share'; }}
        renderMarketShare();
    }});
}});
renderMarketShare();

var showAccess = true, showRes = true, showZones = true, showGap = false, showReentry = false, showAnalysis = false, showDtod = false, showGcar = false, showClosed = false, showMarketShare = false;
function styleBtn(btn, active) {{
    if (active) btn.classList.add('active');
    else btn.classList.remove('active');
}}

document.getElementById('toggleAccess').addEventListener('click', function() {{
    showAccess = !showAccess;
    showAccess ? accessLayer.addTo(map) : map.removeLayer(accessLayer);
    styleBtn(this, showAccess);
}});
document.getElementById('toggleRes').addEventListener('click', function() {{
    showRes = !showRes;
    showRes ? resLayer.addTo(map) : map.removeLayer(resLayer);
    styleBtn(this, showRes);
}});
document.getElementById('toggleDtod').addEventListener('click', function() {{
    showDtod = !showDtod;
    showDtod ? dtodLayer.addTo(map) : map.removeLayer(dtodLayer);
    styleBtn(this, showDtod);
}});
document.getElementById('toggleZones').addEventListener('click', function() {{
    showZones = !showZones;
    showZones ? zoneLayer.addTo(map) : map.removeLayer(zoneLayer);
    styleBtn(this, showZones);
    document.getElementById('zoneTypeFilter').style.display = showZones ? 'block' : 'none';
    if (showZones) applyZoneTypeFilter();
}});
function applyZoneTypeFilter() {{
    var checks = document.querySelectorAll('.zone-type-chk');
    var allowed = [];
    checks.forEach(function(c) {{ if (c.checked) allowed.push(parseInt(c.value)); }});
    zoneLayer.clearLayers();
    allZoneMarkers.forEach(function(m) {{
        if (allowed.indexOf(m._zoneData.imaginary) >= 0) zoneLayer.addLayer(m);
    }});
}}
document.querySelectorAll('.zone-type-chk').forEach(function(c) {{
    c.addEventListener('change', applyZoneTypeFilter);
}});
document.getElementById('toggleGap').addEventListener('click', function() {{
    var wasOn = showGap;
    hidePanels();
    if (!wasOn) {{ showGap = true; document.getElementById('gapPanel').style.display = 'block'; gapLayer.addTo(map); }}
    styleBtn(this, showGap);
}});
document.getElementById('toggleReentry').addEventListener('click', function() {{
    var wasOn = showReentry;
    hidePanels();
    if (!wasOn) {{ showReentry = true; document.getElementById('reentryPanel').style.display = 'block'; reentryLayer.addTo(map); }}
    styleBtn(this, showReentry);
}});

// 공급 분석 테이블
var sortCol = 'status', sortAsc = true;
var analysisTab = 'growth';

function sparkSvg(vals, color) {{
    if (!vals || vals.length < 2) return '';
    var w = 60, h = 18, pad = 1;
    var mn = Math.min.apply(null, vals), mx = Math.max.apply(null, vals);
    var range = mx - mn || 1;
    var pts = vals.map(function(v, i) {{
        var x = pad + i * ((w - 2 * pad) / (vals.length - 1));
        var y = h - pad - (v - mn) / range * (h - 2 * pad);
        return x.toFixed(1) + ',' + y.toFixed(1);
    }});
    return '<svg width="' + w + '" height="' + h + '" style="vertical-align:middle">' +
        '<polyline points="' + pts.join(' ') + '" fill="none" stroke="' + color + '" stroke-width="1.5" stroke-linecap="round"/>' +
        '</svg>';
}}

function switchAnalysisTab(tab) {{
    analysisTab = tab;
    sortCol = 'status'; sortAsc = true;
    document.getElementById('tabGrowth').classList.toggle('active', tab === 'growth');
    document.getElementById('tabDecline').classList.toggle('active', tab === 'decline');
    document.getElementById('analysisDesc').textContent = tab === 'growth'
        ? '예약 점유율 상승 추세 지역 | 증감·그래프 = ' + teamTotalLabel + ' 대비 점유율 변화 (' + teamTotalLabel + ' 행은 절대값)'
        : '예약 점유율 하락 추세 지역 | 증감·그래프 = ' + teamTotalLabel + ' 대비 점유율 변화 (' + teamTotalLabel + ' 행은 절대값)';
    renderAnalysis();
}}

function renderAnalysis() {{
    var data = analysisTab === 'growth' ? analysisGrowth : analysisDecline;
    var regionFilter = document.getElementById('analysisRegionFilter');
    var filterR1 = regionFilter ? regionFilter.value : '';
    var statusOrd = analysisTab === 'growth'
        ? {{'점검 필요':0,'증차 검토':1,'대응 진행 중':2,'-':99}}
        : {{'점검 필요':0,'감차 검토':1,'대응 진행 중':2,'-':99}};
    var emptyMsg = analysisTab === 'growth' ? '수요 성장 지역 없음' : '수요 감소 지역 없음';
    var ggRow = null;
    var rest = [];
    data.forEach(function(d) {{
        if (d.region2 === teamTotalLabel) ggRow = d;
        else if (!filterR1 || d.region1 === filterR1) rest.push(d);
    }});
    rest.sort(function(a, b) {{
        if (sortCol === 'region2') return sortAsc ? a.region2.localeCompare(b.region2) : b.region2.localeCompare(a.region2);
        if (sortCol === 'status') {{
            var d = (statusOrd[a.status]||9) - (statusOrd[b.status]||9);
            if (d !== 0) return sortAsc ? d : -d;
            return analysisTab === 'growth' ? b.res_growth - a.res_growth : a.res_growth - b.res_growth;
        }}
        return sortAsc ? a[sortCol] - b[sortCol] : b[sortCol] - a[sortCol];
    }});
    var sorted = ggRow ? [ggRow].concat(rest) : rest;

    var html = '';
    var statusColors = {{'점검 필요':'#e53935','증차 검토':'#ff9800','감차 검토':'#ff9800','대응 진행 중':'#43a047'}};
    sorted.forEach(function(d, idx) {{
        var sc = statusColors[d.status] || '#8890a4';
        var isGg = d.region2 === teamTotalLabel;
        var rowStyle = isGg ? 'background:#f0f4ff;font-weight:700;' : '';
        var borderStyle = isGg ? 'border-bottom:2px solid #e0e2e6' : 'border-bottom:1px solid #f0f1f3';
        var accColor = d.access_growth >= 0 ? '#0064FF' : '#e53935';
        var resColor = d.res_growth >= 0 ? '#0064FF' : '#e53935';
        var carColor = d.car_growth >= 0 ? '#18a34a' : '#e53935';
        html += '<tr style="' + rowStyle + '">' +
            '<td style="padding:4px 6px;' + borderStyle + ';white-space:nowrap">' + d.region2.replace(/\\u3000/g,' ') + '</td>' +
            '<td style="text-align:right;padding:4px 6px;' + borderStyle + '">' + d.access_weekly.toLocaleString() + '</td>' +
            '<td style="padding:2px 4px;' + borderStyle + '">' + sparkSvg(d.access_trend, accColor) + '</td>' +
            '<td style="text-align:right;padding:4px 6px;' + borderStyle + ';color:' + accColor + ';font-weight:600">' + (d.access_growth >= 0 ? '+' : '') + d.access_growth.toFixed(1) + '%</td>' +
            '<td style="text-align:right;padding:4px 6px;' + borderStyle + '">' + d.res_weekly.toLocaleString() + '</td>' +
            '<td style="padding:2px 4px;' + borderStyle + '">' + sparkSvg(d.res_trend, resColor) + '</td>' +
            '<td style="text-align:right;padding:4px 6px;' + borderStyle + ';color:' + resColor + ';font-weight:600">' + (d.res_growth >= 0 ? '+' : '') + d.res_growth.toFixed(1) + '%</td>' +
            '<td style="text-align:right;padding:4px 6px;' + borderStyle + '">' + d.cars + '</td>' +
            '<td style="padding:2px 4px;' + borderStyle + '">' + sparkSvg(d.car_trend, carColor) + '</td>' +
            '<td style="text-align:right;padding:4px 6px;' + borderStyle + ';color:' + carColor + '">' + (d.car_growth >= 0 ? '+' : '') + d.car_growth.toFixed(1) + '%</td>' +
            (isGg ? '<td style="text-align:center;padding:4px 6px;' + borderStyle + ';color:#8b95a5;font-weight:600">기준선</td>' :
            '<td style="text-align:center;padding:4px 6px;' + borderStyle + '"><span style="background:' + sc + ';color:#fff;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:600;white-space:nowrap;">' + d.status + '</span></td>') +
            '</tr>';
    }});
    if (sorted.length <= 1) html += '<tr><td colspan="11" style="padding:12px;text-align:center;color:#6b7394;">' + emptyMsg + '</td></tr>';
    document.getElementById('analysisBody').innerHTML = html;
}}
document.querySelectorAll('#analysisPanel th').forEach(function(th) {{
    th.addEventListener('click', function() {{
        var col = this.dataset.col;
        if (sortCol === col) sortAsc = !sortAsc;
        else {{ sortCol = col; sortAsc = false; }}
        renderAnalysis();
    }});
}});
renderAnalysis();

function hidePanels() {{
    ['gapPanel','reentryPanel','analysisPanel','marketSharePanel'].forEach(function(id) {{ document.getElementById(id).style.display = 'none'; }});
    showGap = false; showReentry = false; showAnalysis = false; showMarketShare = false;
    map.removeLayer(gapLayer);
    map.removeLayer(reentryLayer);
    styleBtn(document.getElementById('toggleGap'), false);
    styleBtn(document.getElementById('toggleReentry'), false);
    // styleBtn(document.getElementById('toggleAnalysis'), false);  // 비활성화
    styleBtn(document.getElementById('toggleMarketShare'), false);
}}

// toggleAnalysis 비활성화
// document.getElementById('toggleAnalysis').addEventListener('click', function() {{
//     var wasOn = showAnalysis;
//     hidePanels();
//     if (!wasOn) {{ showAnalysis = true; document.getElementById('analysisPanel').style.display = 'block'; }}
//     styleBtn(this, showAnalysis);
// }});

document.getElementById('toggleGcar').addEventListener('click', function() {{
    showGcar = !showGcar;
    showGcar ? gcarLayer.addTo(map) : map.removeLayer(gcarLayer);
    styleBtn(this, showGcar);
}});

document.getElementById('toggleClosed').addEventListener('click', function() {{
    showClosed = !showClosed;
    showClosed ? closedLayer.addTo(map) : map.removeLayer(closedLayer);
    styleBtn(this, showClosed);
}});
// EV 필터: 1=EV운영존, 2=충전보장형 (1과2 상호배제), 3=충전인프라 (독립)
var evFilterMode = 0; // 0=off, 1=EV운영존, 2=충전보장형
var showEvInfra = false;

function applyEvZoneFilter() {{
    // 존 마커 필터링
    allZoneMarkers.forEach(function(m) {{
        var z = m._zoneData;
        if (!z) return;
        if (evFilterMode === 0) {{
            // EV 필터 꺼짐 — 기존 존타입 필터만 적용
            return;
        }}
        if (evFilterMode === 1 && !z.has_ev) {{
            zoneLayer.removeLayer(m);
        }}
        if (evFilterMode === 2 && !z.has_charged_ev) {{
            zoneLayer.removeLayer(m);
        }}
    }});
}}

function resetZoneLayer() {{
    zoneLayer.clearLayers();
    allZoneMarkers.forEach(function(m) {{
        var z = m._zoneData;
        if (!z) return;
        zoneLayer.addLayer(m);
    }});
    applyZoneTypeFilter();
    if (evFilterMode > 0) {{
        applyEvZoneFilter();
        // EV 필터 활성화 시 zoneLayer가 지도에 없으면 추가
        if (!map.hasLayer(zoneLayer)) zoneLayer.addTo(map);
    }}
}}

document.getElementById('toggleEvZones').addEventListener('click', function() {{
    if (evFilterMode === 1) {{
        evFilterMode = 0;
        styleBtn(this, false);
        // EV 필터 해제 시 운영존 토글 상태 복원
        if (!showZones) map.removeLayer(zoneLayer);
    }} else {{
        evFilterMode = 1;
        styleBtn(this, true);
        styleBtn(document.getElementById('toggleChargedOnly'), false);
    }}
    resetZoneLayer();
}});
document.getElementById('toggleChargedOnly').addEventListener('click', function() {{
    if (evFilterMode === 2) {{
        evFilterMode = 0;
        styleBtn(this, false);
        if (!showZones) map.removeLayer(zoneLayer);
    }} else {{
        evFilterMode = 2;
        styleBtn(this, true);
        styleBtn(document.getElementById('toggleEvZones'), false);
    }}
    resetZoneLayer();
}});
document.getElementById('toggleEvInfra').addEventListener('click', function() {{
    showEvInfra = !showEvInfra;
    document.getElementById('evInfraFilter').style.display = showEvInfra ? 'block' : 'none';
    if (showEvInfra) {{ rebuildEvLayer(); evLayer.addTo(map); }}
    else {{ map.removeLayer(evLayer); }}
    styleBtn(this, showEvInfra);
}});
document.querySelectorAll('input[name="evInfraType"]').forEach(function(radio) {{
    radio.addEventListener('change', function() {{
        if (showEvInfra) {{ rebuildEvLayer(); evLayer.addTo(map); }}
    }});
}});

document.getElementById('toggleMarketShare').addEventListener('click', function() {{
    var wasOn = showMarketShare;
    hidePanels();
    if (!wasOn) {{ showMarketShare = true; document.getElementById('marketSharePanel').style.display = 'block'; }}
    styleBtn(this, showMarketShare);
}});

styleBtn(document.getElementById('toggleAccess'), true);
styleBtn(document.getElementById('toggleRes'), true);
styleBtn(document.getElementById('toggleDtod'), false);
styleBtn(document.getElementById('toggleZones'), true);
styleBtn(document.getElementById('toggleGap'), false);
styleBtn(document.getElementById('toggleReentry'), false);
// styleBtn(document.getElementById('toggleAnalysis'), false);  // 비활성화
styleBtn(document.getElementById('toggleGcar'), false);
styleBtn(document.getElementById('toggleClosed'), false);
styleBtn(document.getElementById('toggleEvZones'), false);
styleBtn(document.getElementById('toggleChargedOnly'), false);
styleBtn(document.getElementById('toggleEvInfra'), false);
styleBtn(document.getElementById('toggleMarketShare'), false);

// ── 측정 도구 (거리 재기 + 반경 측정) ──
(function() {{
    var mode = null; // 'distance' | 'radius'
    var measureLayer = L.layerGroup().addTo(map);
    var guide = document.getElementById('measureGuide');
    var distBtn = document.getElementById('toolDistance');
    var radiusBtn = document.getElementById('toolRadius');
    var points = [], polyline = null;
    var radiusCenter = null, radiusCircle = null, radiusLine = null;

    function hav(lat1, lng1, lat2, lng2) {{
        var R = 6371000, dLat = (lat2-lat1)*Math.PI/180, dLng = (lng2-lng1)*Math.PI/180;
        var a = Math.sin(dLat/2)*Math.sin(dLat/2)+Math.cos(lat1*Math.PI/180)*Math.cos(lat2*Math.PI/180)*Math.sin(dLng/2)*Math.sin(dLng/2);
        return R*2*Math.atan2(Math.sqrt(a),Math.sqrt(1-a));
    }}
    function fmt(m) {{ return m>=1000?(m/1000).toFixed(2)+'km':Math.round(m)+'m'; }}

    function clearAll() {{
        measureLayer.clearLayers();
        points=[]; polyline=null; radiusCenter=null; radiusCircle=null; radiusLine=null;
    }}

    function disableMarkerClicks() {{
        zoneLayer.eachLayer(function(l) {{ if (l._icon) l._icon.style.pointerEvents = 'none'; }});
        closedLayer.eachLayer(function(l) {{ if (l._icon) l._icon.style.pointerEvents = 'none'; }});
        gcarLayer.eachLayer(function(l) {{ if (l._icon) l._icon.style.pointerEvents = 'none'; }});
    }}
    function enableMarkerClicks() {{
        zoneLayer.eachLayer(function(l) {{ if (l._icon) l._icon.style.pointerEvents = ''; }});
        closedLayer.eachLayer(function(l) {{ if (l._icon) l._icon.style.pointerEvents = ''; }});
        gcarLayer.eachLayer(function(l) {{ if (l._icon) l._icon.style.pointerEvents = ''; }});
    }}

    function makeMeasureCard(label, value, color) {{
        return '<div style="background:#fff;border-radius:10px;padding:10px 14px;box-shadow:0 2px 12px rgba(0,0,0,0.12);min-width:120px;">' +
            '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">' +
            '<span style="font-size:11px;color:#5a6270;font-weight:600;">' + label + '</span>' +
            '<span style="font-size:13px;color:' + color + ';font-weight:800;">' + value + '</span></div>' +
            '<div class="measure-delete-btn" style="display:flex;align-items:center;justify-content:center;gap:4px;padding:5px 0;border-top:1px solid #f0f1f3;cursor:pointer;font-size:11px;color:#8b95a5;font-weight:500;">' +
            '<span style="font-size:12px;">✂</span> 지우기</div></div>';
    }}

    function stopTool() {{
        mode=null;
        map.off('click', onDistClick); map.off('dblclick', onDistDbl);
        map.off('click', onRadiusClick); map.off('mousemove', onRadiusMove);
        map.doubleClickZoom.enable();
        document.getElementById('map').style.cursor='';
        guide.style.display='none';
        distBtn.style.background='#fff'; distBtn.style.color='#5a6270';
        radiusBtn.style.background='#fff'; radiusBtn.style.color='#5a6270';
        enableMarkerClicks();
    }}

    function activeStyle(btn) {{
        btn.style.background='#0064FF'; btn.style.color='#fff';
    }}

    function bindDeleteBtn(marker) {{
        setTimeout(function() {{
            var el = marker.getElement();
            if (!el) return;
            var btn = el.querySelector('.measure-delete-btn');
            if (btn) btn.addEventListener('click', function(ev) {{
                ev.stopPropagation();
                clearAll();
            }});
        }}, 50);
    }}

    // ─ 거리 재기 ─
    function onDistClick(e) {{
        points.push(e.latlng);
        L.circleMarker(e.latlng, {{ radius:5, color:'#6366f1', fillColor:'#6366f1', fillOpacity:1, weight:2 }}).addTo(measureLayer);
        if (points.length > 1) {{
            var prev=points[points.length-2], cur=e.latlng;
            var seg=hav(prev.lat,prev.lng,cur.lat,cur.lng);
            var total=0; for(var i=1;i<points.length;i++) total+=hav(points[i-1].lat,points[i-1].lng,points[i].lat,points[i].lng);
            if (polyline) measureLayer.removeLayer(polyline);
            polyline=L.polyline(points,{{color:'#6366f1',weight:3,dashArray:'8,6',opacity:0.9}}).addTo(measureLayer);
            // 구간 거리 (작은 라벨)
            L.marker([(prev.lat+cur.lat)/2,(prev.lng+cur.lng)/2],{{ icon:L.divIcon({{ className:'', iconSize:[0,0], html:'<div style="position:relative;left:-50%;background:#fff;color:#6366f1;padding:3px 10px;border-radius:6px;font-size:10px;font-weight:700;box-shadow:0 1px 6px rgba(0,0,0,0.12);white-space:nowrap;display:inline-block;">'+fmt(seg)+'</div>' }}) }}).addTo(measureLayer);
            // 총 거리 카드
            if (points.length > 2 && measureLayer._lastTotalLabel) measureLayer.removeLayer(measureLayer._lastTotalLabel);
            var tl=L.marker(cur,{{ icon:L.divIcon({{ className:'', iconSize:[0,0], html:'<div style="position:relative;left:-50%;top:8px;">' + makeMeasureCard('총거리', fmt(total), '#6366f1') + '</div>' }}) }}).addTo(measureLayer);
            bindDeleteBtn(tl);
            measureLayer._lastTotalLabel = tl;
        }}
    }}
    function onDistDbl(e) {{ L.DomEvent.stopPropagation(e); stopTool(); }}

    // ─ 반경 측정 ─
    function onRadiusClick(e) {{
        if (!radiusCenter) {{
            radiusCenter = e.latlng;
            L.circleMarker(e.latlng, {{ radius:5, color:'#6366f1', fillColor:'#6366f1', fillOpacity:1, weight:2 }}).addTo(measureLayer);
            guide.innerHTML = '지도를 클릭하여 반경 확정 · ESC 취소';
        }} else {{
            var dist = hav(radiusCenter.lat, radiusCenter.lng, e.latlng.lat, e.latlng.lng);
            if (radiusCircle) measureLayer.removeLayer(radiusCircle);
            if (radiusLine) measureLayer.removeLayer(radiusLine);
            if (radiusPreviewLabel) measureLayer.removeLayer(radiusPreviewLabel);
            radiusCircle = L.circle(radiusCenter, {{ radius:dist, color:'#6366f1', fillColor:'#6366f1', fillOpacity:0.06, weight:2, dashArray:'6,4' }}).addTo(measureLayer);
            radiusLine = L.polyline([radiusCenter, e.latlng], {{ color:'#6366f1', weight:2, dashArray:'6,4' }}).addTo(measureLayer);
            L.circleMarker(e.latlng, {{ radius:4, color:'#6366f1', fillColor:'#6366f1', fillOpacity:1, weight:2 }}).addTo(measureLayer);
            // 반경 카드
            var rm = L.marker([(radiusCenter.lat+e.latlng.lat)/2,(radiusCenter.lng+e.latlng.lng)/2], {{ icon:L.divIcon({{ className:'', iconSize:[0,0], html:'<div style="position:relative;left:-50%;">' + makeMeasureCard('총반경', fmt(dist), '#6366f1') + '</div>' }}) }}).addTo(measureLayer);
            bindDeleteBtn(rm);
            map.off('mousemove', onRadiusMove);
            stopTool();
        }}
    }}
    var radiusPreviewLabel = null;
    function onRadiusMove(e) {{
        if (!radiusCenter) return;
        var dist = hav(radiusCenter.lat, radiusCenter.lng, e.latlng.lat, e.latlng.lng);
        if (radiusCircle) measureLayer.removeLayer(radiusCircle);
        if (radiusLine) measureLayer.removeLayer(radiusLine);
        if (radiusPreviewLabel) measureLayer.removeLayer(radiusPreviewLabel);
        radiusCircle = L.circle(radiusCenter, {{ radius:dist, color:'#6366f1', fillColor:'#6366f1', fillOpacity:0.06, weight:2, dashArray:'6,4' }}).addTo(measureLayer);
        radiusLine = L.polyline([radiusCenter, e.latlng], {{ color:'#6366f1', weight:2, dashArray:'6,4', opacity:0.7 }}).addTo(measureLayer);
        radiusPreviewLabel = L.marker([(radiusCenter.lat+e.latlng.lat)/2,(radiusCenter.lng+e.latlng.lng)/2], {{ icon:L.divIcon({{ className:'', iconSize:[0,0], html:'<div style="position:relative;left:-50%;background:#fff;color:#1a1a1a;padding:8px 14px;border-radius:10px;font-size:12px;font-weight:700;box-shadow:0 2px 12px rgba(0,0,0,0.12);white-space:nowrap;display:inline-block;">반경 <span style="color:#6366f1;">' + fmt(dist) + '</span></div>' }}) }}).addTo(measureLayer);
    }}

    distBtn.addEventListener('click', function() {{
        if (mode === 'distance') {{ stopTool(); return; }}
        stopTool(); clearAll(); mode = 'distance';
        activeStyle(distBtn);
        disableMarkerClicks();
        map.doubleClickZoom.disable();
        map.on('click', onDistClick); map.on('dblclick', onDistDbl);
        document.getElementById('map').style.cursor = 'crosshair';
        guide.innerHTML = '클릭으로 포인트 추가 · 더블클릭 종료 · ESC 취소';
        guide.style.display = 'block';
    }});

    radiusBtn.addEventListener('click', function() {{
        if (mode === 'radius') {{ stopTool(); return; }}
        stopTool(); clearAll(); mode = 'radius';
        activeStyle(radiusBtn);
        disableMarkerClicks();
        map.doubleClickZoom.disable();
        map.on('click', onRadiusClick); map.on('mousemove', onRadiusMove);
        document.getElementById('map').style.cursor = 'crosshair';
        guide.innerHTML = '중심점 클릭 · ESC 취소';
        guide.style.display = 'block';
    }});

    document.addEventListener('keydown', function(e) {{
        if (e.key === 'Escape' && mode) {{ stopTool(); clearAll(); }}
    }});
}})();

// 업데이트 공통 함수
function doUpdate(endpoint, btn, label) {{
    if (!confirm(label + '를 실행하시겠습니까?\\nBigQuery 조회 후 페이지가 새로고침됩니다.')) return;
    btn.disabled = true;
    btn.classList.add('updating');
    btn.textContent = label + ' 중...';
    fetch(endpoint, {{ method: 'POST', headers: {{'Content-Type':'application/json'}}, body: JSON.stringify({{team_id: TEAM_ID}}) }})
        .then(function(r) {{ return r.json(); }})
        .then(function(data) {{
            if (data.success) {{
                alert(label + ' 완료! 페이지를 새로고침합니다.');
                location.reload();
            }} else {{
                alert(label + ' 실패: ' + (data.error || '알 수 없는 오류'));
                btn.disabled = false; btn.classList.remove('updating'); btn.textContent = label;
            }}
        }})
        .catch(function(e) {{
            alert('서버에 연결할 수 없습니다.\\nserver.py가 실행 중인지 확인하세요.');
            btn.disabled = false; btn.classList.remove('updating'); btn.textContent = label;
        }});
}}
function runUpdateDemand() {{
    doUpdate('/api/update-demand', document.getElementById('updateDemandBtn'), '수요 업데이트');
}}
function runUpdateZone() {{
    doUpdate('/api/update-zone', document.getElementById('updateZoneBtn'), '존/실적 업데이트');
}}

// 예약 타임라인 렌더링
var wayColors = {{ 'round':'#5c6bc0', 'handle':'#5c6bc0', 'd2d_oneway':'#00897b', 'd2d_round':'#43a047', 'd2d_rev':'#43a047', 'z2d_oneway':'#00897b', 'block':'#555c6e' }};
var wayLabels = {{ 'round':'', 'handle':'', 'd2d_oneway':'부름', 'd2d_round':'부름', 'd2d_rev':'부름', 'z2d_oneway':'부름', 'block':'블락' }};
function buildTimelineTable(reservations, days, now, startDate, endDate) {{
    var dayNames = ['일','월','화','수','목','금','토'];
    var cars = {{}};
    var carOrder = [];
    reservations.forEach(function(r) {{
        var key = r.car_id;
        if (!cars[key]) {{ cars[key] = {{ car_name: r.car_name, car_num: r.car_num, reservations: [] }}; carOrder.push(key); }}
        cars[key].reservations.push(r);
    }});
    var numDays = days.length;
    var html = '<table style="border-collapse:collapse;font-size:10px;width:100%;min-width:600px;color:#1a1a1a;table-layout:fixed;">';
    html += '<colgroup><col style="width:110px;min-width:110px;">';
    for (var ci = 0; ci < numDays; ci++) html += '<col style="width:' + (100/numDays).toFixed(2) + '%;">';
    html += '</colgroup>';
    html += '<thead><tr><th style="padding:4px 6px;border-bottom:2px solid #e8eaed;position:sticky;left:0;background:#fff;z-index:1;color:#8b95a5;font-weight:600;">차량</th>';
    days.forEach(function(day) {{
        var mm = String(day.getMonth()+1).padStart(2,'0');
        var dd = String(day.getDate()).padStart(2,'0');
        var dn = dayNames[day.getDay()];
        var isToday = day.toDateString() === now.toDateString();
        var isWeekend = day.getDay() === 0 || day.getDay() === 6;
        var bg = isToday ? '#fffde7' : isWeekend ? '#fff5f5' : '#fff';
        html += '<th style="padding:3px 1px;border-bottom:2px solid #e8eaed;font-size:9px;background:' + bg + ';color:#8b95a5;font-weight:600;">' + mm + '-' + dd + '<br>(' + dn + ')</th>';
    }});
    html += '</tr></thead><tbody>';
    carOrder.forEach(function(carId) {{
        var car = cars[carId];
        html += '<tr><td style="padding:4px 6px;border-bottom:1px solid #f0f1f3;white-space:nowrap;position:sticky;left:0;background:#fff;z-index:1;font-size:9px;"><b style="color:#1a1a1a">' + car.car_name + '</b><br><span style="color:#8b95a5">' + car.car_num + '</span></td>';
        days.forEach(function(day, di) {{
            var dayStart = new Date(day); dayStart.setHours(0,0,0,0);
            var dayEnd = new Date(day); dayEnd.setHours(23,59,59,999);
            var isToday = day.toDateString() === now.toDateString();
            var isWeekend = day.getDay() === 0 || day.getDay() === 6;
            var bg = isToday ? '#fffde7' : isWeekend ? '#fff5f5' : '#fafbfc';
            html += '<td style="padding:0;border-bottom:1px solid #f0f1f3;border-left:1px solid #f4f5f7;border-right:1px solid #f4f5f7;height:30px;position:relative;overflow:visible;background:' + bg + '">';
            car.reservations.forEach(function(rv) {{
                var rs = new Date(rv.start.replace(' ', 'T'));
                var re = new Date(rv.end.replace(' ', 'T'));
                if (re < dayStart || rs > dayEnd) return;
                var isFirstDay = rs >= dayStart;
                if (!isFirstDay && di > 0) return;
                var barStartPx = Math.max(0, (rs - dayStart) / 86400000) * 100;
                var totalSpanMs = Math.min(endDate.getTime(), re.getTime()) - Math.max(startDate.getTime(), rs.getTime());
                var barWidthDays = totalSpanMs / 86400000;
                var barWidthPct = barWidthDays * 100;
                if (barWidthPct < 5) barWidthPct = 5;
                var color = rv.block ? wayColors['block'] : (wayColors[rv.way] || '#90a4ae');
                var lbl = rv.block ? wayLabels['block'] : (wayLabels[rv.way] || '');
                var tipWay = rv.block ? '블락' : rv.way;
                html += '<div title="' + rv.start + ' ~ ' + rv.end + ' (' + tipWay + ')" style="position:absolute;top:2px;bottom:2px;left:' + barStartPx.toFixed(1) + '%;width:' + barWidthPct.toFixed(1) + '%;background:' + color + ';border-radius:2px;opacity:0.85;overflow:hidden;color:#fff;font-size:7px;line-height:24px;text-align:center;cursor:default;z-index:1;pointer-events:auto;">' + lbl + '</div>';
            }});
            html += '</td>';
        }});
        html += '</tr>';
    }});
    html += '</tbody></table>';
    return html;
}}

function showTimeline(zoneId, zoneName, evZoneId, evZoneName) {{
    var panel = document.getElementById('timelinePanel');
    var content = document.getElementById('timelineContent');
    document.getElementById('timelineTitle').textContent = zoneName + ' — 예약 현황';
    var reservations = timelineData[String(zoneId)] || [];
    var evReservations = evZoneId ? (timelineData[String(evZoneId)] || []) : [];

    if (reservations.length === 0 && evReservations.length === 0) {{
        content.innerHTML = '<div style="color:#999;padding:20px;text-align:center">예약 데이터 없음</div>';
        panel.style.display = 'block';
        return;
    }}
    var now = new Date();
    var startDate = new Date(now); startDate.setDate(startDate.getDate() - 7); startDate.setHours(0,0,0,0);
    var endDate = new Date(now); endDate.setDate(endDate.getDate() + 7); endDate.setHours(23,59,59,999);
    var days = [];
    for (var d = new Date(startDate); d <= endDate; d.setDate(d.getDate() + 1)) {{
        days.push(new Date(d));
    }}

    var html = '';
    // 원본존 타임라인
    if (reservations.length > 0) {{
        html += '<div style="font-size:11px;color:#8b95a5;font-weight:500;margin-bottom:6px;">차량별 예약 타임라인 (전후 1주)</div>';
        html += buildTimelineTable(reservations, days, now, startDate, endDate);
    }}
    // EV 가상존 타임라인
    if (evReservations.length > 0) {{
        html += '<div style="margin-top:14px;padding-top:10px;border-top:2px solid #1565c0;">';
        html += '<div style="display:flex;align-items:center;gap:6px;margin-bottom:6px;"><span style="background:#1565c0;color:#fff;font-size:9px;font-weight:800;padding:2px 6px;border-radius:3px;">충전보장 EV</span><span style="font-size:11px;color:#8b95a5;font-weight:500;">' + evZoneName + '</span></div>';
        html += buildTimelineTable(evReservations, days, now, startDate, endDate);
        html += '</div>';
    }} else if (evZoneId) {{
        html += '<div style="margin-top:14px;padding-top:10px;border-top:2px solid #1565c0;">';
        html += '<div style="display:flex;align-items:center;gap:6px;"><span style="background:#1565c0;color:#fff;font-size:9px;font-weight:800;padding:2px 6px;border-radius:3px;">충전보장 EV</span><span style="font-size:11px;color:#999;">' + evZoneName + ' — 예약 없음</span></div>';
        html += '</div>';
    }}
    // 범례
    html += '<div style="margin-top:10px;display:flex;gap:12px;font-size:10px;flex-wrap:wrap;color:#8b95a5;font-weight:500;">';
    [['handle','일반','#5c6bc0'],['d2d_round','부름왕복','#43a047'],['d2d_oneway','부름편도','#00897b'],['block','블락','#555c6e']].forEach(function(x) {{
        html += '<span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:' + x[2] + ';vertical-align:middle;margin-right:3px;"></span>' + x[1] + '</span>';
    }});
    html += '</div>';
    content.innerHTML = html;
    panel.style.display = 'block';
}}

// ── 존 개설 시뮬레이션 ──
(function() {{
    var simCtx = document.getElementById('simCtx');
    var simPanel = document.getElementById('simPanel');
    var simOverlay = document.getElementById('simOverlay');
    var simContent = document.getElementById('simContent');
    var simMarker = null;
    var simLat, simLng;

    // 우클릭 → 컨텍스트 메뉴
    map.on('contextmenu', function(e) {{
        e.originalEvent.preventDefault();
        simLat = e.latlng.lat;
        simLng = e.latlng.lng;
        simCtx.style.left = e.originalEvent.pageX + 'px';
        simCtx.style.top = e.originalEvent.pageY + 'px';
        simCtx.style.display = 'block';
    }});

    map.on('click', function() {{ simCtx.style.display = 'none'; }});
    map.on('movestart', function() {{ simCtx.style.display = 'none'; }});

    document.getElementById('simCtxBtn').addEventListener('click', function() {{
        simCtx.style.display = 'none';
        runSimulation(simLat, simLng);
    }});

    document.getElementById('simClose').addEventListener('click', closeSimPanel);
    simOverlay.addEventListener('click', closeSimPanel);

    function closeSimPanel() {{
        simPanel.style.display = 'none';
        simOverlay.style.display = 'none';
        if (simMarker) {{ map.removeLayer(simMarker); simMarker = null; }}
    }}

    function runSimulation(lat, lng) {{
        // 시뮬레이션 마커
        if (simMarker) map.removeLayer(simMarker);
        simMarker = L.marker([lat, lng], {{
            icon: L.divIcon({{
                className: 'zone-marker-wrap',
                html: '<div class="sim-marker-pin">?</div>',
                iconSize: [32, 32], iconAnchor: [16, 16]
            }})
        }}).addTo(map);

        // 로딩 표시 (팝업은 숨긴 상태, 오버레이만 표시)
        simPanel.style.display = 'none';
        simOverlay.style.display = 'block';
        // 로딩 토스트
        var loadingToast = document.createElement('div');
        loadingToast.id = 'simLoadingToast';
        loadingToast.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:2001;background:#fff;border-radius:16px;padding:32px 40px;box-shadow:0 8px 32px rgba(0,0,0,0.15);text-align:center;';
        loadingToast.innerHTML = '<div class="sim-marker-pin" style="display:inline-flex;width:44px;height:44px;font-size:18px;margin-bottom:14px;">?</div><div id="simLoadingMsg" style="color:#1a1a1a;font-size:14px;font-weight:700;">시뮬레이션 진행 중</div><div id="simLoadingSub" style="color:#8b95a5;font-size:11px;margin-top:4px;">반경 500m 실시간 수요 집계 중...</div>';
        document.body.appendChild(loadingToast);

        var isLocal = (location.hostname === 'localhost' || location.hostname === '127.0.0.1' || location.hostname.endsWith('.ngrok-free.dev'));
        var simApiBase = isLocal ? '' : '{ngrok_url}';

        // 1단계: BQ 시뮬레이션
        fetch(simApiBase + '/api/simulate', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json', 'ngrok-skip-browser-warning': 'true' }},
            body: JSON.stringify({{ lat: lat, lng: lng, radius: 0.5, team_id: TEAM_ID }})
        }})
        .then(function(resp) {{
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            return resp.json();
        }})
        .then(function(d) {{
            if (d.error) throw new Error(d.error);
            // 2단계: AI 분석
            document.getElementById('simLoadingMsg').textContent = 'AI 분석 중';
            document.getElementById('simLoadingSub').textContent = 'GPT · Claude 종합 평가 생성 중...';
            return fetch(simApiBase + '/api/simulate-eval', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json', 'ngrok-skip-browser-warning': 'true' }},
                body: JSON.stringify(Object.assign({{}}, d, {{team_id: TEAM_ID}}))
            }})
            .then(function(r) {{ return r.json(); }})
            .then(function(ev) {{ return {{ sim: d, eval: ev }}; }})
            .catch(function() {{ return {{ sim: d, eval: null }}; }});
        }})
        .then(function(result) {{
            // 로딩 토스트 제거, 결과 팝업 표시
            var toast = document.getElementById('simLoadingToast');
            if (toast) toast.remove();
            renderSimResult(result.sim, result.eval);
            simPanel.style.display = 'block';
        }})
        .catch(function(err) {{
            var toast = document.getElementById('simLoadingToast');
            if (toast) toast.remove();
            simContent.innerHTML = '<div style="color:#e74c3c;padding:20px;text-align:center;font-weight:600;">시뮬레이션 실패<br><span style="font-size:10px;color:#8b95a5;font-weight:400;">' + err.message + '</span></div>';
            simPanel.style.display = 'block';
        }});
    }}

    function renderSimResult(d, ev) {{
        var gradeColors = {{ 'S': '#0064FF', 'A': '#18a34a', 'B': '#e07800', 'F': '#e53935' }};
        var gradeBgs = {{ 'S': '#f0f4ff', 'A': '#edfcf2', 'B': '#fff7ed', 'F': '#fef2f2' }};
        var gc = gradeColors[d.demand_grade] || '#8b95a5';
        var gb = gradeBgs[d.demand_grade] || '#f7f8fa';

        // AI 추천 합의 계산
        function checkRecommend(text) {{
            if (!text) return null;
            var first = text.split('\\n')[0].trim();
            if (first.indexOf('비추천') >= 0 || first.indexOf('추천 X') >= 0 || first.indexOf('추천X') >= 0) return false;
            if (first.indexOf('추천 O') >= 0 || first.indexOf('추천O') >= 0 || first.indexOf('추천합니다') >= 0) return true;
            return null;
        }}
        var gptRec = ev ? checkRecommend(ev.gpt) : null;
        var claudeRec = ev ? checkRecommend(ev.claude) : null;
        var aiConsensus = 'unknown';
        if (gptRec === true && claudeRec === true) aiConsensus = 'recommend';
        else if (gptRec === false && claudeRec === false) aiConsensus = 'not_recommend';
        else if (gptRec !== null || claudeRec !== null) aiConsensus = 'hold';

        // ── 전체 2컬럼 시작 ──
        var html = '<div style="display:flex;gap:20px;align-items:flex-start;">';
        // ── 좌측 ──
        html += '<div style="flex:1;min-width:0;">';

        // 최종 결과 카드
        html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px;">';

        // 수요 등급
        html += '<div style="background:' + gb + ';border-radius:12px;padding:14px 16px;text-align:center;">';
        html += '<div style="font-size:10px;color:#8b95a5;font-weight:600;margin-bottom:6px;">수요 등급</div>';
        html += '<div style="font-size:32px;font-weight:900;color:' + gc + ';line-height:1;">' + d.demand_grade + '</div>';
        html += '<div style="font-size:10px;color:#8b95a5;margin-top:4px;">상위 ' + (100 - d.demand_percentile).toFixed(0) + '%</div>';
        html += '</div>';

        // 예상 주간 예약
        html += '<div style="background:#f7f8fa;border-radius:12px;padding:14px 16px;text-align:center;">';
        html += '<div style="font-size:10px;color:#8b95a5;font-weight:600;margin-bottom:6px;">예상 주간 예약</div>';
        html += '<div style="font-size:28px;font-weight:900;color:#1a1a1a;line-height:1;">' + d.est_weekly_res.toLocaleString() + '<span style="font-size:14px;font-weight:600;">건</span></div>';
        html += '<div style="font-size:10px;color:#8b95a5;margin-top:4px;">접속 ' + d.base_res + ' + 부름 ' + d.dtod_additional + '</div>';
        html += '</div>';

        // 추천 공급대수
        html += '<div style="background:#f0f4ff;border-radius:12px;padding:14px 16px;text-align:center;">';
        html += '<div style="font-size:10px;color:#8b95a5;font-weight:600;margin-bottom:6px;">추천 공급대수</div>';
        html += '<div style="font-size:28px;font-weight:900;color:#0064FF;line-height:1;">' + d.recommended_cars + '<span style="font-size:14px;font-weight:600;">대</span></div>';
        html += '<div style="font-size:10px;color:#8b95a5;margin-top:4px;">적정 ' + d.raw_recommended_cars + ' - 카니발 ' + d.cannibal_cars + '</div>';
        html += '</div>';

        // AI 종합 추천
        var aiBg, aiColor, aiLabel, aiIcon;
        if (aiConsensus === 'recommend') {{ aiBg = '#edfcf2'; aiColor = '#18a34a'; aiLabel = '개설 추천'; aiIcon = 'O'; }}
        else if (aiConsensus === 'not_recommend') {{ aiBg = '#fef2f2'; aiColor = '#e53935'; aiLabel = '개설 비추천'; aiIcon = 'X'; }}
        else if (aiConsensus === 'hold') {{ aiBg = '#fffbeb'; aiColor = '#d97706'; aiLabel = '보류'; aiIcon = '△'; }}
        else {{ aiBg = '#f7f8fa'; aiColor = '#8b95a5'; aiLabel = 'AI 판단 불가'; aiIcon = '-'; }}
        html += '<div style="background:' + aiBg + ';border-radius:12px;padding:14px 16px;text-align:center;">';
        html += '<div style="font-size:10px;color:#8b95a5;font-weight:600;margin-bottom:6px;">AI 추천</div>';
        html += '<div style="font-size:28px;font-weight:900;color:' + aiColor + ';line-height:1;">' + aiIcon + '</div>';
        html += '<div style="font-size:10px;color:' + aiColor + ';margin-top:4px;font-weight:600;">' + aiLabel + '</div>';
        html += '</div></div>';

        // 상세 정보
        html += '<div style="font-size:11px;color:#8b95a5;margin-bottom:6px;">' + d.region2 + ' ' + d.region3 + ' · ' + d.lat.toFixed(5) + ', ' + d.lng.toFixed(5);
        if (d.nearest_zone) html += ' · 최근접: ' + d.nearest_zone + ' (' + d.nearest_zone_dist_km + 'km)';
        html += '</div>';

        // 과거 존 이력
        if (d.hist_zones && d.hist_zones.length > 0) {{
            var normalZones = d.hist_zones.filter(function(hz) {{ return !hz.is_d2d_call; }});
            var callZones = d.hist_zones.filter(function(hz) {{ return hz.is_d2d_call; }});
            html += '<div style="background:#f5f0ff;border-radius:12px;padding:14px 16px;margin:10px 0;border:1px solid #e8dff5;">';
            html += '<div style="font-size:10px;font-weight:700;color:#7c3aed;margin-bottom:8px;">과거 존 이력 (반경 300m) · 대당매출 30% 블렌딩 반영</div>';
            if (normalZones.length > 0) {{
                html += '<div style="font-size:10px;font-weight:700;color:#5a6270;margin-bottom:6px;">일반존</div>';
                normalZones.forEach(function(hz) {{
                    html += '<div style="font-size:12px;margin-bottom:6px;line-height:1.5;"><b>' + hz.zone_name + '</b> ' + hz.first_date + '~' + hz.last_date + ' (' + hz.operation_days + '일) · 대당매출 <b style="color:#e07800">' + (hz.revenue_per_car_28d > 0 ? hz.revenue_per_car_28d.toLocaleString() + '원' : '-') + '</b>/4주</div>';
                }});
            }}
            if (callZones.length > 0) {{
                if (normalZones.length > 0) html += '<div style="border-top:1px solid #e8dff5;margin:8px 0;"></div>';
                html += '<div style="font-size:10px;font-weight:700;color:#5a6270;margin-bottom:6px;">부름호출존</div>';
                callZones.forEach(function(hz) {{
                    html += '<div style="font-size:12px;margin-bottom:6px;line-height:1.5;"><b>' + hz.zone_name + '</b> ' + hz.first_date + '~' + hz.last_date + ' (' + hz.operation_days + '일) · 월평균 <b style="color:#0064FF">' + (hz.monthly_avg_res > 0 ? hz.monthly_avg_res.toLocaleString() + '건' : '-') + '</b> · 건당 <b style="color:#e07800">' + (hz.revenue_per_res > 0 ? hz.revenue_per_res.toLocaleString() + '원' : '-') + '</b></div>';
                }});
            }}
            html += '</div>';
        }}

        // 수요 + 벤치마크
        html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">';
        html += '<div style="background:#f7f8fa;border-radius:10px;padding:10px 12px;">';
        html += '<div style="font-size:10px;font-weight:700;color:#8b95a5;margin-bottom:6px;">반경 500m 수요 (주간)</div>';
        html += '<div class="sim-row"><span class="sim-label">접속</span><span class="sim-value">' + d.weekly_access.toLocaleString() + '</span></div>';
        html += '<div class="sim-row"><span class="sim-label">예약</span><span class="sim-value">' + d.weekly_res.toLocaleString() + '</span></div>';
        html += '<div class="sim-row"><span class="sim-label">부름</span><span class="sim-value">' + d.weekly_dtod.toLocaleString() + '</span></div>';
        var convCtx = d.conv_rank ? ' <span style="font-size:9px;color:#8b95a5;font-weight:400;">(' + d.region2 + ' ' + d.conv_total_r3 + '개 중 ' + d.conv_rank + '위, 평균 ' + d.r2_avg_conv.toFixed(2) + '%)</span>' : '';
        html += '<div class="sim-row"><span class="sim-label">전환율</span><span class="sim-value">' + (d.conv_rate * 100).toFixed(2) + '%' + convCtx + '</span></div>';
        html += '</div>';
        html += '<div style="background:#f7f8fa;border-radius:10px;padding:10px 12px;">';
        html += '<div style="font-size:10px;font-weight:700;color:#8b95a5;margin-bottom:6px;">인근 벤치마크 (3km, ' + d.bench_zone_count + '존)</div>';
        html += '<div class="sim-row"><span class="sim-label">대당매출</span><span class="sim-value">' + (d.est_rev_per_car > 0 ? d.est_rev_per_car.toLocaleString() + '원' : '-') + '</span></div>';
        html += '<div class="sim-row"><span class="sim-label">대당GP</span><span class="sim-value">' + (d.est_gp_per_car > 0 ? d.est_gp_per_car.toLocaleString() + '원' : '-') + '</span></div>';
        html += '<div class="sim-row"><span class="sim-label">가동률</span><span class="sim-value">' + (d.avg_util > 0 ? d.avg_util.toFixed(1) + '%' : '-') + '</span></div>';
        html += '<div class="sim-row"><span class="sim-label">건당매출</span><span class="sim-value">' + (d.revenue_per_res > 0 ? d.revenue_per_res.toLocaleString() + '원' : '-') + '</span></div>';
        html += '</div></div>';

        // 주차비 분석
        if (d.parking_mid > 0) {{
            html += '<div style="background:#f7f8fa;border-radius:10px;padding:12px 14px;margin-top:8px;">';
            html += '<div style="font-size:10px;font-weight:700;color:#8b95a5;margin-bottom:8px;">주차비 분석</div>';
            html += '<div class="sim-row"><span class="sim-label">인근 평균 주차비</span><span class="sim-value">' + (d.bench_avg_parking > 0 ? d.bench_avg_parking.toLocaleString() + '원/월' : '-') + ' <span style="font-size:9px;color:#8b95a5;font-weight:400;">(' + d.bench_parking_count + '개 존)</span></span></div>';
            if (d.hist_avg_parking > 0) {{
                html += '<div class="sim-row"><span class="sim-label">과거 이력 (물가보정)</span><span class="sim-value">' + d.hist_avg_parking.toLocaleString() + '원/월</span></div>';
            }}
            html += '<div class="sim-row" style="margin-top:4px;padding-top:4px;border-top:1px solid #e8eaed;"><span class="sim-label" style="color:#0064FF;font-weight:700;">추천 범위</span><span class="sim-value" style="color:#0064FF;">' + d.parking_range_low.toLocaleString() + ' ~ ' + d.parking_range_high.toLocaleString() + '원</span></div>';
            html += '</div>';

            // GPM 시뮬레이션
            if (d.gpm_scenarios && d.gpm_scenarios.length > 0) {{
                html += '<div style="background:#f7f8fa;border-radius:10px;padding:12px 14px;margin-top:8px;">';
                html += '<div style="font-size:10px;font-weight:700;color:#8b95a5;margin-bottom:8px;">GPM 시뮬레이션 (주차비별)</div>';
                html += '<table style="width:100%;border-collapse:collapse;font-size:11px;">';
                html += '<thead><tr><th style="text-align:left;padding:4px 6px;border-bottom:2px solid #e0e2e6;color:#8b95a5;font-weight:600;">주차비/월</th><th style="text-align:right;padding:4px 6px;border-bottom:2px solid #e0e2e6;color:#8b95a5;font-weight:600;">대당GP</th><th style="text-align:right;padding:4px 6px;border-bottom:2px solid #e0e2e6;color:#8b95a5;font-weight:600;">GPM</th></tr></thead><tbody>';
                d.gpm_scenarios.forEach(function(s) {{
                    var isMid = s.is_mid;
                    var rowBg = isMid ? 'background:#f0f4ff;' : '';
                    var gpColor = s.gp_per_car < 0 ? '#e53935' : '#1a1a1a';
                    var gpmColor = s.gpm < 0 ? '#e53935' : s.gpm >= 20 ? '#18a34a' : '#1a1a1a';
                    html += '<tr style="' + rowBg + '">';
                    html += '<td style="padding:5px 6px;border-bottom:1px solid #f0f1f3;">' + s.parking_cost.toLocaleString() + '원' + (isMid ? ' <span style="color:#0064FF;font-size:9px;font-weight:700;">추천</span>' : '') + '</td>';
                    html += '<td style="text-align:right;padding:5px 6px;border-bottom:1px solid #f0f1f3;color:' + gpColor + ';font-weight:600;">' + s.gp_per_car.toLocaleString() + '원</td>';
                    html += '<td style="text-align:right;padding:5px 6px;border-bottom:1px solid #f0f1f3;color:' + gpmColor + ';font-weight:700;">' + s.gpm.toFixed(1) + '%</td>';
                    html += '</tr>';
                }});
                html += '</tbody></table></div>';
            }}
        }}

        html += '</div>';  // 좌측 끝

        // ── 우측: AI 평가 ──
        html += '<div style="flex:1;min-width:0;">';

        // ── AI 평가 (동기) ──
        var gptLogo = '<svg width="16" height="16" viewBox="0 0 24 24" style="vertical-align:-3px;margin-right:4px;"><path fill="#1a1a1a" d="M22.282 9.821a5.985 5.985 0 0 0-.516-4.91 6.046 6.046 0 0 0-6.51-2.9A6.065 6.065 0 0 0 4.981 4.18a5.998 5.998 0 0 0-3.992 2.9 6.049 6.049 0 0 0 .743 7.097 5.98 5.98 0 0 0 .51 4.911 6.051 6.051 0 0 0 6.515 2.9A5.985 5.985 0 0 0 13.26 24a6.056 6.056 0 0 0 5.772-4.206 5.99 5.99 0 0 0 3.997-2.9 6.056 6.056 0 0 0-.747-7.073zM13.26 22.43a4.476 4.476 0 0 1-2.876-1.04l.141-.081 4.779-2.758a.795.795 0 0 0 .392-.681v-6.737l2.02 1.168a.071.071 0 0 1 .038.052v5.583a4.504 4.504 0 0 1-4.494 4.494zM3.6 18.304a4.47 4.47 0 0 1-.535-3.014l.142.085 4.783 2.759a.771.771 0 0 0 .78 0l5.843-3.369v2.332a.08.08 0 0 1-.033.062L9.74 19.95a4.5 4.5 0 0 1-6.14-1.646zM2.34 7.896a4.485 4.485 0 0 1 2.366-1.973V11.6a.766.766 0 0 0 .388.676l5.815 3.355-2.02 1.168a.076.076 0 0 1-.071 0l-4.83-2.786A4.504 4.504 0 0 1 2.34 7.872zm16.597 3.855l-5.833-3.387L15.119 7.2a.076.076 0 0 1 .071 0l4.83 2.791a4.494 4.494 0 0 1-.676 8.105v-5.678a.79.79 0 0 0-.407-.667zm2.01-3.023l-.141-.085-4.774-2.782a.776.776 0 0 0-.785 0L9.409 9.23V6.897a.066.066 0 0 1 .028-.061l4.83-2.787a4.5 4.5 0 0 1 6.68 4.66zm-12.64 4.135l-2.02-1.164a.08.08 0 0 1-.038-.057V6.075a4.5 4.5 0 0 1 7.375-3.453l-.142.08L8.704 5.46a.795.795 0 0 0-.393.681zm1.097-2.365l2.602-1.5 2.607 1.5v2.999l-2.597 1.5-2.607-1.5z"/></svg>';
        var claudeLogo = '<svg width="16" height="16" viewBox="0 0 400 400" style="vertical-align:-3px;margin-right:4px;"><path fill="#D97757" d="m124.011 241.251 49.164-27.585.826-2.396-.826-1.333h-2.396l-8.217-.506-28.09-.759-24.363-1.012-23.603-1.266-5.938-1.265L75 197.79l.574-3.661 4.994-3.358 7.153.625 15.808 1.079 23.722 1.637 17.208 1.012 25.493 2.649h4.049l.574-1.637-1.384-1.012-1.079-1.012-24.548-16.635-26.573-17.58-13.919-10.123-7.524-5.129-3.796-4.808-1.637-10.494 6.833-7.525 9.178.624 2.345.625 9.296 7.153 19.858 15.37 25.931 19.098 3.796 3.155 1.519-1.08.185-.759-1.704-2.851-14.104-25.493-15.049-25.931-6.698-10.747-1.772-6.445c-.624-2.649-1.08-4.876-1.08-7.592l7.778-10.561L144.729 75l10.376 1.383 4.37 3.797 6.445 14.745 10.443 23.215 16.197 31.566 4.741 9.364 2.53 8.672.945 2.649h1.637v-1.519l1.332-17.782 2.464-21.832 2.395-28.091.827-7.912 3.914-9.482 7.778-5.129 6.074 2.902 4.994 7.153-.692 4.623-2.969 19.301-5.821 30.234-3.796 20.245h2.21l2.531-2.53 10.241-13.599 17.208-21.511 7.593-8.537 8.857-9.431 5.686-4.488h10.747l7.912 11.76-3.543 12.147-11.067 14.037-9.178 11.895-13.16 17.714-8.216 14.172.759 1.131 1.957-.186 29.727-6.327 16.062-2.901 19.166-3.29 8.672 4.049.944 4.116-3.408 8.419-20.498 5.062-24.042 4.808-35.801 8.469-.439.321.506.624 16.13 1.519 6.9.371h16.888l31.448 2.345 8.217 5.433 4.926 6.647-.827 5.061-12.653 6.445-17.074-4.049-39.85-9.482-13.666-3.408h-1.889v1.131l11.388 11.135 20.87 18.845 26.133 24.295 1.333 6.006-3.357 4.741-3.543-.506-22.962-17.277-8.858-7.777-20.06-16.888H238.5v1.771l4.623 6.765 24.413 36.696 1.265 11.253-1.771 3.661-6.327 2.21-6.951-1.265-14.29-20.06-14.745-22.591-11.895-20.246-1.451.827-7.018 75.601-3.29 3.863-7.592 2.902-6.327-4.808-3.357-7.778 3.357-15.37 4.049-20.06 3.29-15.943 2.969-19.807 1.772-6.58-.118-.439-1.451.186-14.931 20.498-22.709 30.689-17.968 19.234-4.302 1.704-7.458-3.864.692-6.9 4.167-6.141 24.869-31.634 14.999-19.605 9.684-11.32-.068-1.637h-.573l-66.052 42.887-11.759 1.519-5.062-4.741.625-7.778 2.395-2.531 19.858-13.665-.068.067z"/></svg>';
        if (ev) {{
            var parseEvalText = function(text) {{
                if (!text) return {{ recommend: null, body: text || '평가 실패' }};
                var lines = text.split('\\n');
                var first = lines[0].trim();
                var isO = first.indexOf('추천 O') >= 0 || first.indexOf('추천O') >= 0 || (first.indexOf('추천') >= 0 && first.indexOf('비추천') < 0);
                var isX = first.indexOf('비추천') >= 0 || first.indexOf('추천 X') >= 0 || first.indexOf('추천X') >= 0;
                if (isO && !isX) return {{ recommend: true, body: lines.slice(1).join('\\n').trim() }};
                if (isX) return {{ recommend: false, body: lines.slice(1).join('\\n').trim() }};
                return {{ recommend: null, body: text }};
            }};
            var gptP = parseEvalText(ev.gpt);
            var claudeP = parseEvalText(ev.claude);

            html += '<div style="margin-top:14px;border-top:1px solid #f0f1f3;padding-top:14px;">';
            html += '<div style="font-size:13px;font-weight:800;color:#1a1a1a;margin-bottom:10px;">AI 종합 평가</div>';
            html += '<div style="display:flex;flex-direction:column;gap:8px;">';
            // GPT
            var gBadge = gptP.recommend === true ? '<span style="background:#edfcf2;color:#18a34a;padding:3px 10px;border-radius:8px;font-size:11px;font-weight:700;">개설 추천</span>' : gptP.recommend === false ? '<span style="background:#fef2f2;color:#e53935;padding:3px 10px;border-radius:8px;font-size:11px;font-weight:700;">개설 비추천</span>' : '';
            html += '<div style="background:#f7f8fa;border-radius:10px;padding:12px 14px;border-left:3px solid #1a1a1a;">';
            html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;"><span style="font-size:11px;font-weight:700;color:#1a1a1a;">' + gptLogo + 'GPT</span>' + gBadge + '</div>';
            html += '<div style="font-size:12px;color:#1a1a1a;line-height:1.7;white-space:pre-wrap;">' + gptP.body + '</div></div>';
            // Claude
            var cBadge = claudeP.recommend === true ? '<span style="background:#edfcf2;color:#18a34a;padding:3px 10px;border-radius:8px;font-size:11px;font-weight:700;">개설 추천</span>' : claudeP.recommend === false ? '<span style="background:#fef2f2;color:#e53935;padding:3px 10px;border-radius:8px;font-size:11px;font-weight:700;">개설 비추천</span>' : '';
            html += '<div style="background:#fdf8f4;border-radius:10px;padding:12px 14px;border-left:3px solid #D97757;">';
            html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;"><span style="font-size:11px;font-weight:700;color:#D97757;">' + claudeLogo + 'Claude</span>' + cBadge + '</div>';
            html += '<div style="font-size:12px;color:#1a1a1a;line-height:1.7;white-space:pre-wrap;">' + claudeP.body + '</div></div>';
            html += '</div></div>';
        }}

        html += '</div>';  // 우측 끝
        html += '</div>';  // 전체 2컬럼 끝

        // ── 산출 기준 (토글) ──
        html += '<div style="margin-top:14px;border-top:1px solid #f0f1f3;padding-top:10px;">';
        html += '<div id="simCriteriaToggle" style="cursor:pointer;display:flex;align-items:center;gap:6px;font-size:11px;color:#8b95a5;font-weight:600;">';
        html += '<span id="simCriteriaArrow">▶</span> 산출 기준 상세</div>';
        html += '<div id="simCriteriaBody" style="display:none;margin-top:10px;background:#f7f8fa;border-radius:10px;padding:14px 16px;font-size:11px;color:#5a6270;line-height:1.7;">';
        html += '<b style="color:#0064FF">수요 등급</b> — ' + teamTotalLabel + ' 접속 격자 중 백분위 (S:상위5%, A:상위20%, B:상위50%, F:하위50%)<br>';
        html += '<b style="color:#0064FF">전환율</b> — region3 우선, region2 fallback. 근 3개월 월별 전환율 중 <b>최소값</b> 적용<br>';
        html += '<b style="color:#0064FF">예상 예약</b> — 접속수 × 전환율 + 부름 × 50%<br>';
        html += '<b style="color:#0064FF">벤치마크</b> — 반경 3km 내 존들의 대당매출·GP·가동률 거리역수 가중평균<br>';
        html += '<b style="color:#0064FF">적정대수</b> — 추정 월 매출(예약 × 4주 × 건당매출) ÷ 대당목표 160만원<br>';
        html += '<b style="color:#0064FF">카니발</b> — 반경 500m 기존 존 차량 거리가중 차감<br>';
        html += '<b style="color:#0064FF">과거 이력</b> — 반경 100m 폐존 실적 30% + 현재추정 70% 블렌딩<br>';
        html += '<b style="color:#0064FF">주차비</b> — 인근 벤치마크 가중평균 + 과거 이력(CPI 3% 물가보정) 블렌딩<br>';
        html += '<b style="color:#0064FF">GPM</b> — GP = 벤치마크GP - (시나리오주차비 - 벤치마크평균주차비), GPM = GP/매출×100<br>';
        html += '<b style="color:#0064FF">AI 추천</b> — GPT+Claude 모두 추천→O, 한쪽만→보류, 모두 비추천→X';
        html += '</div></div>';

        simContent.innerHTML = html;
        document.getElementById('simCriteriaToggle').addEventListener('click', function() {{
            var body = document.getElementById('simCriteriaBody');
            var arrow = document.getElementById('simCriteriaArrow');
            if (body.style.display === 'none') {{
                body.style.display = 'block';
                arrow.textContent = '▼';
            }} else {{
                body.style.display = 'none';
                arrow.textContent = '▶';
            }}
        }});

    }}
}})();

// ── 부름호출지역 표시 ──
map.createPane('d2dPane');
map.getPane('d2dPane').style.zIndex = 650;
var d2dDestLayer = L.layerGroup([], {{ pane: 'd2dPane' }}).addTo(map);
var d2dShowZones = false;
function d2dHideLayers() {{
    var pane = map.getPane('overlayPane');
    if (pane) pane.style.opacity = '0.15';
    var marker = map.getPane('markerPane');
    if (marker) marker.style.opacity = '0';
    var shadow = map.getPane('shadowPane');
    if (shadow) shadow.style.opacity = '0';
    // tooltipPane, popupPane 은 유지 (측정 도구용)
}}
function d2dRestoreLayers() {{
    ['overlayPane','markerPane','shadowPane'].forEach(function(name) {{
        var p = map.getPane(name);
        if (p) p.style.opacity = '';
    }});
    d2dShowZones = false;
}}
function d2dToggleZones() {{
    d2dShowZones = !d2dShowZones;
    var marker = map.getPane('markerPane');
    if (marker) marker.style.opacity = d2dShowZones ? '1' : '0';
    var btn = document.getElementById('d2dZoneToggle');
    if (btn) {{
        btn.style.background = d2dShowZones ? '#0064FF' : '#f4f5f7';
        btn.style.color = d2dShowZones ? '#fff' : '#5a6270';
    }}
}}
function showD2dDestinations(zoneId, zoneName, evZoneId) {{
    d2dDestLayer.clearLayers();
    map.closePopup();
    var isLocal = (location.hostname === 'localhost' || location.hostname === '127.0.0.1' || location.hostname.endsWith('.ngrok-free.dev'));
    var apiBase = isLocal ? '' : '{ngrok_url}';

    var loadDiv = document.createElement('div');
    loadDiv.id = 'd2dLoading';
    loadDiv.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:2001;background:#fff;border-radius:12px;padding:20px 28px;box-shadow:0 4px 20px rgba(0,0,0,0.12);text-align:center;font-size:12px;color:#5a6270;font-weight:600;';
    loadDiv.textContent = '부름호출지역 조회 중...';
    document.body.appendChild(loadDiv);

    // 원본존 + EV 가상존 동시 조회
    var fetches = [
        fetch(apiBase + '/api/d2d-destinations', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json', 'ngrok-skip-browser-warning': 'true' }},
            body: JSON.stringify({{ zone_id: zoneId, team_id: TEAM_ID }})
        }}).then(function(r) {{ return r.json(); }})
    ];
    if (evZoneId && evZoneId > 0) {{
        fetches.push(
            fetch(apiBase + '/api/d2d-destinations', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json', 'ngrok-skip-browser-warning': 'true' }},
                body: JSON.stringify({{ zone_id: evZoneId, team_id: TEAM_ID }})
            }}).then(function(r) {{ return r.json(); }})
        );
    }}

    Promise.all(fetches)
    .then(function(results) {{
        var ld = document.getElementById('d2dLoading');
        if (ld) ld.remove();
        var data = results[0];
        var evData = results[1] || {{ destinations: [], total: 0 }};
        var allEmpty = (!data.destinations || data.destinations.length === 0) && (!evData.destinations || evData.destinations.length === 0);
        if (allEmpty) {{
            alert('부름 호출 데이터가 없습니다.');
            return;
        }}

        d2dHideLayers();
        var bounds = [];
        var zone = zonesData.find(function(z) {{ return z.zone_id === zoneId; }});

        // 원본존 부름 (빨강)
        (data.destinations || []).forEach(function(d) {{
            var r = Math.min(25, Math.max(8, d.cnt * 3));
            L.circleMarker([d.lat, d.lng], {{
                radius: r, color: '#c62828', fillColor: '#ef5350',
                fillOpacity: 0.6, weight: 2, opacity: 0.9, pane: 'd2dPane'
            }}).bindTooltip('<div style="font-weight:600;">' + d.address + '</div><div style="color:#c62828;">' + d.cnt + '건 · ' + d.way + '</div>', {{ direction: 'top', pane: 'd2dPane' }})
            .addTo(d2dDestLayer);
            bounds.push([d.lat, d.lng]);
        }});

        // EV 가상존 부름 (파랑)
        (evData.destinations || []).forEach(function(d) {{
            var r = Math.min(25, Math.max(8, d.cnt * 3));
            L.circleMarker([d.lat, d.lng], {{
                radius: r, color: '#0d47a1', fillColor: '#42a5f5',
                fillOpacity: 0.6, weight: 2, opacity: 0.9, pane: 'd2dPane'
            }}).bindTooltip('<div style="font-weight:600;">' + d.address + '</div><div style="color:#0d47a1;">⚡ EV ' + d.cnt + '건 · ' + d.way + '</div>', {{ direction: 'top', pane: 'd2dPane' }})
            .addTo(d2dDestLayer);
            bounds.push([d.lat, d.lng]);
        }});

        // 존 위치 마커 + 연결선
        if (zone) {{
            bounds.push([zone.lat, zone.lng]);
            L.marker([zone.lat, zone.lng], {{
                pane: 'd2dPane',
                icon: L.divIcon({{
                    className: '',
                    iconSize: [0, 0],
                    html: '<div style="position:relative;left:-50%;bottom:30px;white-space:nowrap;"><div style="display:inline-block;background:#e53935;color:#fff;padding:5px 12px;border-radius:8px;font-size:11px;font-weight:700;box-shadow:0 2px 8px rgba(0,0,0,0.2);">📍 ' + zoneName + ' (출발)</div><div style="width:2px;height:20px;background:#e53935;margin:0 auto;"></div></div>'
                }})
            }}).addTo(d2dDestLayer);
            // 원본존 연결선 (보라)
            (data.destinations || []).slice(0, 15).forEach(function(d) {{
                L.polyline([[zone.lat, zone.lng], [d.lat, d.lng]], {{
                    color: '#ef5350', weight: 2, opacity: 0.4, dashArray: '6,4', pane: 'd2dPane'
                }}).addTo(d2dDestLayer);
            }});
            // EV 연결선 (파랑)
            (evData.destinations || []).slice(0, 15).forEach(function(d) {{
                L.polyline([[zone.lat, zone.lng], [d.lat, d.lng]], {{
                    color: '#42a5f5', weight: 2, opacity: 0.4, dashArray: '3,6', pane: 'd2dPane'
                }}).addTo(d2dDestLayer);
            }});
        }}

        if (bounds.length > 0) map.fitBounds(bounds, {{ padding: [40, 40] }});

        // 상단 배너
        var totalCnt = (data.total || 0) + (evData.total || 0);
        var evLabel = (evData.total > 0) ? ' + <span style="color:#1565c0;">⚡EV ' + evData.total + '건</span>' : '';
        var closeDiv = document.createElement('div');
        closeDiv.id = 'd2dBanner';
        closeDiv.style.cssText = 'position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:1000;background:#fff;border-radius:12px;padding:10px 20px;box-shadow:0 2px 12px rgba(0,0,0,0.12);display:flex;align-items:center;gap:12px;font-size:12px;';
        closeDiv.innerHTML = '<span style="font-weight:700;color:#6366f1;">' + zoneName + '</span> 부름호출지역 <span style="color:#8b95a5;">(<span style="color:#6366f1;">' + (data.total||0) + '건</span>' + evLabel + ')</span>' +
            (evData.total > 0 ? '<span style="display:flex;gap:8px;font-size:10px;color:#8b95a5;"><span><span style="display:inline-block;width:8px;height:8px;background:#ef5350;border-radius:50%;vertical-align:middle;margin-right:2px;"></span>일반</span><span><span style="display:inline-block;width:8px;height:8px;background:#42a5f5;border-radius:50%;vertical-align:middle;margin-right:2px;"></span>EV</span></span>' : '') +
            '<button id="d2dZoneToggle" onclick="d2dToggleZones()" style="padding:5px 14px;border:none;border-radius:8px;background:#f4f5f7;color:#5a6270;font-size:11px;font-weight:600;cursor:pointer;">운영 존 보기</button>' +
            '<button id="d2dCloseBtn" style="padding:5px 14px;border:none;border-radius:8px;background:#0064FF;color:#fff;font-size:11px;font-weight:600;cursor:pointer;">닫기</button>';
        document.body.appendChild(closeDiv);
        document.getElementById('d2dCloseBtn').addEventListener('click', function() {{
            d2dDestLayer.clearLayers();
            d2dRestoreLayers();
            var banner = document.getElementById('d2dBanner');
            if (banner) banner.remove();
        }});
    }})
    .catch(function(err) {{
        var ld = document.getElementById('d2dLoading');
        if (ld) ld.remove();
        d2dRestoreLayers();
        alert('부름호출지역 조회 실패: ' + err.message);
    }});
}}

// (검색 기능으로 대체됨)
</script>
</body>
</html>"""
    return html


def regenerate_from_cache(team_id='gyeonggi'):
    """캐시 데이터로 HTML만 재생성 (BQ 조회 없음, 서울 필터 적용)"""
    cache_dir = _team_cache_dir(team_id)
    team_name = TEAM_CONFIG[team_id]['name']
    print(f"[{datetime.now()}] 캐시에서 HTML 재생성 ({team_name})")

    access = _load_cache("access", team_id) or []
    reservation = _load_cache("reservation", team_id) or []
    zones = _load_cache("zones", team_id) or []
    gaps = _load_cache("gaps", team_id) or []
    analysis = _load_cache("supply_demand", team_id) or {}
    dtod = _load_cache("dtod", team_id) or []

    # 서울+인천 데이터 제거 (히트맵: 접경 허용 / 분석: 철저 제외)
    zone_coords = [(float(z['lat']), float(z['lng'])) for z in zones]
    before_a, before_r, before_d = len(access), len(reservation), len(dtod)
    access = filter_non_gyeonggi(access, zone_coords=zone_coords, keep_dist_km=1.0, team_id=team_id)
    reservation = filter_non_gyeonggi(reservation, zone_coords=zone_coords, keep_dist_km=1.0, team_id=team_id)
    dtod = filter_non_gyeonggi(dtod, zone_coords=zone_coords, keep_dist_km=1.0, team_id=team_id)
    print(f"  지역 필터: 접속 {before_a}->{len(access)}, 예약 {before_r}->{len(reservation)}, 부름 {before_d}->{len(dtod)}")

    # 공급 분석: 캐시에서 주간 추이 로드
    weekly_data = _load_cache("weekly_trends", team_id)
    if weekly_data:
        analysis = compute_growth_analysis(weekly_data, zones, team_name=team_name)
    else:
        analysis = _load_cache("supply_demand", team_id) or {}  # fallback
    if isinstance(analysis, dict):
        print(f"  공급 분석: {len(analysis.get('growth',[]))} 성장 / {len(analysis.get('decline',[]))} 감소 지역")
    else:
        print(f"  공급 분석: {len(analysis)} 지역 (레거시)")

    # Gap 분석
    gaps = compute_gaps(access, reservation, zones, team_id=team_id)
    print(f"  Gap 재계산: {len(gaps)} 지역 (역지오코딩 중...)")
    gaps = reverse_geocode(gaps, team_id=team_id, zones_data=zones)
    print(f"  Gap 역지오코딩 완료: {len(gaps)} 지역")

    # 실적 데이터 로드
    profit_data = _load_cache("profit", team_id) or {}
    profit_data = {int(k): v for k, v in profit_data.items()} if profit_data else {}

    # 그린카 데이터 로드
    gcar_data = _load_cache("gcar", team_id) or []

    # 폐쇄 존 데이터 로드
    closed_data = _load_cache("closed", team_id) or []

    # 주차 계약 로드
    parking_contract = _load_cache("parking_contract", team_id) or {}

    # 재진입 추천구역 계산 + 거래처 정보 매칭
    reentry_data = compute_reentry_zones(closed_data, access, reservation, zones_data=zones, team_id=team_id)
    pc_map = {int(k): v for k, v in parking_contract.items()} if parking_contract else {}
    for r in reentry_data:
        pc = pc_map.get(int(r.get('zone_id', 0)), {})
        r['provider_name'] = pc.get('provider_name', '')
        r['settlement_type'] = pc.get('settlement_type', '')
        r['price_per_car'] = pc.get('price_per_car', 0)
    print(f"  재진입 추천구역: {len(reentry_data)} 지역")

    # 전기차 충전소 데이터 로드/조회
    ev_data = _load_cache("ev_chargers", team_id)
    if ev_data is None:
        try:
            ev_data = query_ev_chargers(team_id=team_id)
            _save_cache("ev_chargers", ev_data, team_id)
        except Exception as e:
            print(f"  [경고] 충전소 데이터 조회 실패: {e}")
            ev_data = []
    else:
        print(f"  충전소 캐시: {len(ev_data)}개 충전소")
    # EV 가상존 매핑 (충전소 매칭보다 먼저 — 충전보장존 판별에 필요)
    ev_vz = _load_cache("ev_virtual_zones", team_id)
    if ev_vz is None:
        try:
            ev_vz = query_ev_virtual_zones(team_id=team_id)
            _save_cache("ev_virtual_zones", ev_vz, team_id)
        except Exception as e:
            print(f"  [경고] EV 가상존 조회 실패: {e}")
            ev_vz = {}
    else:
        if isinstance(ev_vz, list):
            ev_vz = {}
        print(f"  EV 가상존 캐시: {len(ev_vz)}개")
    # 존 데이터에 EV 가상존 정보 추가
    for z in zones:
        zid = int(z.get('zone_id', 0))
        if zid in ev_vz:
            z['ev_zone'] = ev_vz[zid]
        elif isinstance(ev_vz, dict):
            # string key fallback
            z['ev_zone'] = ev_vz.get(str(zid), None)

    # EV 차량 보유 존 매핑
    ev_cars = _load_cache("ev_car_zones", team_id)
    if ev_cars is None:
        try:
            ev_cars = query_ev_car_zones(team_id=team_id)
            _save_cache("ev_car_zones", ev_cars, team_id)
        except Exception as e:
            print(f"  [경고] EV 차량 존 조회 실패: {e}")
            ev_cars = {}
    else:
        print(f"  EV 차량 존 캐시: {len(ev_cars)}개")
    for z in zones:
        zid = int(z.get('zone_id', 0))
        ec = ev_cars.get(zid, ev_cars.get(str(zid), {}))
        z['ev_car_count'] = ec.get('ev_car_count', 0)
        z['ev_charged_count'] = ec.get('ev_charged_count', 0)
        z['has_ev'] = z['ev_car_count'] > 0 or z.get('ev_zone') is not None
        z['has_charged_ev'] = z.get('ev_zone') is not None or '전기차' in z.get('zone_name', '')

    # 충전 혼잡도 집계
    ev_congestion = compute_ev_congestion()
    # ev_data에 혼잡도 추가
    for e in ev_data:
        e['congestion'] = ev_congestion.get(e['statId'], None)

    # 존에 충전소 매칭 (ev_zone 설정 후 실행 — 충전보장존 판별에 사용)
    match_ev_to_zones(ev_data, zones)

    # 쏘카 지역별 실 운영 차량 로드
    socar_supply = _load_cache("socar_supply", team_id) or {}

    # 예약 타임라인 로드
    timeline_data = _load_cache("timeline", team_id) or {}

    # 히트맵: 행정구역 polygon 필터 + 동적 격자 수 제한
    access = filter_by_polygon(access, team_id)
    reservation = filter_by_polygon(reservation, team_id)
    dtod = filter_by_polygon(dtod, team_id)

    grid_limit = _calc_grid_limit(len(zones))
    access = sorted(access, key=lambda x: -int(x['access_count']))[:grid_limit]
    reservation = sorted(reservation, key=lambda x: -int(x['reservation_count']))[:grid_limit]
    dtod = sorted(dtod, key=lambda x: -int(x['call_count']))  # HAVING >= 12 already applied

    html = generate_index(access, reservation, zones, gaps, analysis, dtod, profit_data,
                          gcar_data=gcar_data, socar_supply=socar_supply, parking_contract=parking_contract,
                          timeline_data=timeline_data, closed_data=closed_data, reentry_data=reentry_data, ev_data=ev_data, team_id=team_id)
    out_dir = _team_output_dir(team_id)
    path = os.path.join(out_dir, "index.html")
    with open(path, "w") as f:
        f.write(html)
    print(f"  -> {path} ({len(html):,} bytes)")
    # 단일 팀이면 root index.html에도 복사
    root_index = os.path.join(OUTPUT_DIR, "index.html")
    if path != root_index:
        import shutil
        shutil.copy2(path, root_index)
        print(f"  -> {root_index} (복사)")
    print("[완료] HTML 재생성 완료")


def main(team_id='gyeonggi'):
    team_name = TEAM_CONFIG[team_id]['name']
    print(f"[{datetime.now()}] {team_name} 잠재 수요 지도 업데이트 시작")
    print(f"  기간: {THREE_MONTHS_AGO} ~ {TODAY}")

    cache_dir = _team_cache_dir(team_id)

    print("  1/6 앱 접속 데이터 조회...")
    access = query_access(team_id=team_id)
    print(f"       {len(access)} rows, {sum(int(r['access_count']) for r in access):,} 건")

    print("  2/6 예약 생성 데이터 조회...")
    reservation = query_reservation(team_id=team_id)
    print(f"       {len(reservation)} rows, {sum(int(r['reservation_count']) for r in reservation):,} 건")

    print("  3/6 운영 존 + 차량 대수 조회...")
    zones = query_zones(team_id=team_id)
    print(f"       {len(zones)} 존, {sum(int(z.get('car_count',0)) for z in zones):,} 대")

    print("  4/6 지역별 실 예약 건수 조회 (존 소속 차량 기준)...")
    zone_reservations = query_reservation_by_zone_region(team_id=team_id)
    print(f"       {len(zone_reservations)} 지역")

    print("  5/6 부름 호출 위치 조회...")
    dtod = query_dtod(team_id=team_id)
    print(f"       {len(dtod)} rows, {sum(r['call_count'] for r in dtod):,} 건")

    print("  5.1/6 존별 실적 조회 (profit_socar_car_daily)...")
    profit_data = query_zone_profit(team_id=team_id)
    print(f"       {len(profit_data)} 존 실적")

    print("  5.1b/6 주간 실적 집계 (대시보드용)...")
    dashboard_metrics = query_dashboard_metrics(team_id=team_id)
    print(f"       {len(dashboard_metrics.get('weekly', []))} 주 데이터")

    print("  5.1c/6 지역별(region2) 실적 집계...")
    dashboard_region2 = query_dashboard_by_region(team_id=team_id, region_level='region2')
    print(f"       {len(dashboard_region2.get('weekly', []))} rows")

    print("  5.1d/6 지역별(region3) 실적 집계...")
    dashboard_region3 = query_dashboard_by_region(team_id=team_id, region_level='region3')
    print(f"       {len(dashboard_region3.get('weekly', []))} rows")

    print("  5.2/6 그린카 존 현황 조회 (gcar_info_log)...")
    gcar_data = query_gcar_zones(team_id=team_id)
    print(f"       {len(gcar_data)} 존, {sum(int(g.get('total_cars',0)) for g in gcar_data):,} 대")

    print("  5.2b/6 폐쇄 존 현황 조회...")
    closed_data = query_closed_zones(team_id=team_id)
    print(f"       {len(closed_data)} 존")

    print("  5.3/6 쏘카 지역별 실 운영 차량/존 수 (profit_car_daily)...")
    socar_supply = query_socar_supply_by_region(team_id=team_id)
    print(f"       {len(socar_supply)} 지역, {sum(v['socar_cars'] for v in socar_supply.values()):,} 대")

    zone_coords = [(float(z['lat']), float(z['lng'])) for z in zones]

    # 공급 분석: 주간 추이 기반 성장 지역 분석
    print("  5.5/6 주간 추이 조회 (접속/예약/공급)...")
    weekly_data = query_weekly_trends(team_id=team_id)
    access_cnt = len(weekly_data.get('access', []))
    res_cnt = len(weekly_data.get('res', []))
    supply_cnt = len(weekly_data.get('supply', []))
    print(f"       접속 {access_cnt}, 예약 {res_cnt}, 공급 {supply_cnt} rows")
    analysis = compute_growth_analysis(weekly_data, zones, team_name=team_name)
    print(f"       공급 분석: {len(analysis.get('growth',[]))} 성장 / {len(analysis.get('decline',[]))} 감소 지역")

    # Gap 분석
    print("  6/6 미충족 수요 분석 + 역지오코딩...")
    gaps = compute_gaps(access, reservation, zones, team_id=team_id)
    gaps_named = reverse_geocode(gaps, team_id=team_id, zones_data=zones)
    print(f"       {len(gaps_named)} 미충족 지역")

    # 원본 데이터 보관 (캐시 저장용)
    access_raw, reservation_raw, dtod_raw = access[:], reservation[:], dtod[:]

    # 히트맵: 행정구역 polygon 필터 + 동적 격자 수 제한
    access_before = len(access)
    access = filter_by_polygon(access, team_id)
    reservation = filter_by_polygon(reservation, team_id)
    dtod = filter_by_polygon(dtod, team_id)

    grid_limit = _calc_grid_limit(len(zones))
    access.sort(key=lambda x: -int(x['access_count']))
    access = access[:grid_limit]
    reservation.sort(key=lambda x: -int(x['reservation_count']))
    reservation = reservation[:grid_limit]
    dtod.sort(key=lambda x: -int(x['call_count']))
    dtod = dtod[:grid_limit]
    print(f"       polygon 필터: {access_before} -> {len(access)} (top {grid_limit})")

    print("  7/7 예약 타임라인 조회...")
    timeline_data = query_reservation_timeline(team_id=team_id)
    total_res = sum(len(v) for v in timeline_data.values())
    print(f"       {len(timeline_data)} 존, {total_res:,} 건")

    # Save cache (원본 데이터 저장)
    for name, data in [("access", access_raw), ("reservation", reservation_raw),
                       ("zones", zones), ("gaps", gaps_named),
                       ("supply_demand", analysis), ("weekly_trends", weekly_data),
                       ("dtod", dtod_raw),
                       ("profit", profit_data), ("gcar", gcar_data),
                       ("closed", closed_data),
                       ("socar_supply", socar_supply), ("timeline", timeline_data),
                       ("dashboard_metrics", dashboard_metrics),
                       ("dashboard_region2", dashboard_region2),
                       ("dashboard_region3", dashboard_region3),
                       ("advance_util", query_advance_utilization(team_id=team_id))]:
        _save_cache(name, data, team_id=team_id)

    print("  HTML 생성...")
    html1 = generate_index(access, reservation, zones, gaps_named, analysis, dtod, profit_data,
                           gcar_data=gcar_data, socar_supply=socar_supply, timeline_data=timeline_data, closed_data=closed_data,
                           team_id=team_id)
    out_dir = _team_output_dir(team_id)
    path1 = os.path.join(out_dir, "index.html")
    with open(path1, "w") as f:
        f.write(html1)
    print(f"  -> {path1} ({len(html1):,} bytes)")
    print(f"[완료] 대시보드가 생성되었습니다.")


def _load_cache(name, team_id='gyeonggi'):
    path = os.path.join(_team_cache_dir(team_id), f"{name}.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    # fallback: 기존 경로 (마이그레이션 호환)
    old_path = os.path.join(OUTPUT_DIR, ".cache", f"{name}.json")
    if team_id == 'gyeonggi' and os.path.exists(old_path):
        with open(old_path) as f:
            return json.load(f)
    return None


def _save_cache(name, data, team_id='gyeonggi'):
    cache_dir = _team_cache_dir(team_id)
    with open(os.path.join(cache_dir, f"{name}.json"), "w") as f:
        json.dump(data, f, ensure_ascii=False)


def _build_html(team_id='gyeonggi'):
    """캐시에서 모든 데이터 로드 → HTML 생성"""
    access = _load_cache("access", team_id) or []
    reservation = _load_cache("reservation", team_id) or []
    zones = _load_cache("zones", team_id) or []
    gaps = _load_cache("gaps", team_id) or []
    analysis = _load_cache("supply_demand", team_id) or []
    dtod = _load_cache("dtod", team_id) or []
    profit_data = _load_cache("profit", team_id) or {}
    if profit_data:
        profit_data = {int(k): v for k, v in profit_data.items()}
    gcar_data = _load_cache("gcar", team_id) or []
    closed_data = _load_cache("closed", team_id) or []
    socar_supply = _load_cache("socar_supply", team_id) or {}
    parking_contract = _load_cache("parking_contract", team_id) or {}
    if parking_contract:
        parking_contract = {int(k): v for k, v in parking_contract.items()}
    timeline_data = _load_cache("timeline", team_id) or {}

    # 히트맵: 행정구역 polygon 필터 + 동적 격자 수 제한
    access = filter_by_polygon(access, team_id)
    reservation = filter_by_polygon(reservation, team_id)
    dtod = filter_by_polygon(dtod, team_id)
    grid_limit = _calc_grid_limit(len(zones))
    access = sorted(access, key=lambda x: -int(x['access_count']))[:grid_limit]
    reservation = sorted(reservation, key=lambda x: -int(x['reservation_count']))[:grid_limit]
    dtod = sorted(dtod, key=lambda x: -int(x['call_count']))[:grid_limit]

    html = generate_index(access, reservation, zones, gaps, analysis, dtod, profit_data,
                          gcar_data=gcar_data, socar_supply=socar_supply,
                          parking_contract=parking_contract, timeline_data=timeline_data, closed_data=closed_data,
                          team_id=team_id)
    out_dir = _team_output_dir(team_id)
    path = os.path.join(out_dir, "index.html")
    with open(path, "w") as f:
        f.write(html)
    print(f"  -> {path} ({len(html):,} bytes)")


def update_demand(team_id='gyeonggi'):
    """수요 데이터만 업데이트 (앱 접속, 예약 생성, 부름 호출)"""
    team_name = TEAM_CONFIG[team_id]['name']
    print(f"[{datetime.now()}] 수요 데이터 업데이트 ({team_name})")
    print(f"  기간: {THREE_MONTHS_AGO} ~ {TODAY}")

    print("  1/3 앱 접속 데이터 조회...")
    access = query_access(team_id=team_id)
    print(f"       {len(access)} rows")

    print("  2/3 예약 생성 데이터 조회...")
    reservation = query_reservation(team_id=team_id)
    print(f"       {len(reservation)} rows")

    print("  3/3 부름 호출 위치 조회...")
    dtod = query_dtod(team_id=team_id)
    print(f"       {len(dtod)} rows")

    # 존 데이터 로드 (캐시)
    zones = _load_cache("zones", team_id) or []
    zone_coords = [(float(z['lat']), float(z['lng'])) for z in zones]

    # 공급 분석: 주간 추이 기반
    print("  3.5/3 주간 추이 조회...")
    weekly_data = query_weekly_trends(team_id=team_id)
    analysis = compute_growth_analysis(weekly_data, zones, team_name=team_name)
    print(f"       {len(analysis.get('growth',[]))} 성장 / {len(analysis.get('decline',[]))} 감소 지역")

    # Gap 분석
    gaps = compute_gaps(access, reservation, zones, team_id=team_id)
    gaps = reverse_geocode(gaps, team_id=team_id, zones_data=zones)

    # 캐시에 원본 저장 (히트맵 필터 전)
    for name, data in [("access", access), ("reservation", reservation), ("dtod", dtod),
                       ("supply_demand", analysis), ("weekly_trends", weekly_data), ("gaps", gaps)]:
        _save_cache(name, data, team_id=team_id)

    _build_html(team_id=team_id)
    print("[완료] 수요 데이터 업데이트 완료")


def update_zone(team_id='gyeonggi'):
    """존/실적 데이터만 업데이트 (존 정보, 실적, 그린카, 예약 타임라인)"""
    team_name = TEAM_CONFIG[team_id]['name']
    print(f"[{datetime.now()}] 존/실적 데이터 업데이트 ({team_name})")

    print("  1/6 운영 존 + 차량 대수 조회...")
    zones = query_zones(team_id=team_id)
    print(f"       {len(zones)} 존, {sum(int(z.get('car_count',0)) for z in zones):,} 대")

    print("  2/6 존별 실적 조회...")
    profit_data = query_zone_profit(team_id=team_id)
    print(f"       {len(profit_data)} 존 실적")

    print("  3/6 주차 계약 정보 조회...")
    parking_contract = query_parking_contract(team_id=team_id)
    print(f"       {len(parking_contract)} 존 계약")

    print("  4/6 그린카 존 현황 조회...")
    gcar_data = query_gcar_zones(team_id=team_id)
    print(f"       {len(gcar_data)} 존")

    print("  4b/6 폐쇄 존 현황 조회...")
    closed_data = query_closed_zones(team_id=team_id)
    print(f"       {len(closed_data)} 존")

    print("  5/6 쏘카 지역별 실 운영 차량/존 수...")
    socar_supply = query_socar_supply_by_region(team_id=team_id)
    print(f"       {len(socar_supply)} 지역")

    print("  6/6 예약 타임라인 조회...")
    timeline_data = query_reservation_timeline(team_id=team_id)
    print(f"       {len(timeline_data)} 존, {sum(len(v) for v in timeline_data.values()):,} 건")

    print("  7 EV 충전소 조회...")
    ev_chargers = query_ev_chargers(team_id=team_id)

    print("  8 EV 가상존 조회...")
    ev_virtual = query_ev_virtual_zones(team_id=team_id)

    print("  9 EV 차량 존 조회...")
    ev_car_zones = query_ev_car_zones(team_id=team_id)

    for name, data in [("zones", zones), ("profit", profit_data), ("parking_contract", parking_contract),
                       ("gcar", gcar_data), ("closed", closed_data), ("socar_supply", socar_supply), ("timeline", timeline_data),
                       ("ev_chargers", ev_chargers), ("ev_virtual_zones", ev_virtual), ("ev_car_zones", ev_car_zones)]:
        _save_cache(name, data, team_id=team_id)

    # 대시보드 주간 지표 생성 (dashboard.html 의존)
    print("  +1 대시보드 주간 지표 생성...")
    dashboard_metrics = query_dashboard_metrics(team_id=team_id)
    print(f"       {len(dashboard_metrics.get('weekly', []))} 주 데이터")
    _save_cache("dashboard_metrics", dashboard_metrics, team_id=team_id)

    print("  +2 지역별(region2) 주간 지표 생성...")
    dashboard_region2 = query_dashboard_by_region(team_id=team_id, region_level='region2')
    print(f"       {len(dashboard_region2.get('weekly', []))} rows")
    _save_cache("dashboard_region2", dashboard_region2, team_id=team_id)

    print("  +3 지역별(region3) 주간 지표 생성...")
    dashboard_region3 = query_dashboard_by_region(team_id=team_id, region_level='region3')
    print(f"       {len(dashboard_region3.get('weekly', []))} rows")
    _save_cache("dashboard_region3", dashboard_region3, team_id=team_id)

    print("  +4 사전가동률 조회 (W+1, W+2)...")
    try:
        advance_util = query_advance_utilization(team_id=team_id)
        _save_cache("advance_util", advance_util, team_id=team_id)
        now = advance_util.get('now', {})
        w1 = now.get('w1', {})
        w2 = now.get('w2', {})
        if w1:
            print(f"       W+1 주중={w1.get('weekday', 0):.1%} W+2 주중={w2.get('weekday', 0):.1%}")
        else:
            print("       데이터 없음")
    except Exception as e:
        print(f"  [경고] 사전가동률 조회 실패: {e}")

    _build_html(team_id=team_id)
    print("[완료] 존/실적 업데이트 완료")


def generate_landing_page():
    """팀 선택 랜딩 페이지 생성"""
    team_icons = {
        'seoul': '🏙️', 'gyeonggi': '🏔️', 'chungcheong': '🌾',
        'gyeongbuk': '🏛️', 'gyeongnam': '🌊', 'honam': '🌿',
    }
    cards = ''
    for tid, cfg in TEAM_CONFIG.items():
        regions_str = ', '.join(cfg['regions'])
        icon = team_icons.get(tid, '📍')
        cards += f'''
        <a href="/{tid}/" class="team-card">
            <div class="team-icon">{icon}</div>
            <div class="team-info">
                <div class="team-name">{cfg['name']}</div>
                <div class="team-regions">{regions_str}</div>
            </div>
            <div class="team-arrow">→</div>
        </a>'''

    html = f'''<!DOCTYPE html>
<html lang="ko"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>쏘카 수요/인프라 지도</title>
<link href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css" rel="stylesheet">
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Pretendard',-apple-system,sans-serif; background:#fff; min-height:100vh; }}
.hero {{
    background: linear-gradient(135deg, #0064FF 0%, #0046b8 100%);
    padding: 60px 20px 48px;
    text-align: center;
    color: #fff;
}}
.hero-logo {{
    width: 48px; height: 48px; object-fit: contain;
    filter: brightness(10);
    margin-bottom: 16px;
}}
.hero h1 {{ font-size: 28px; font-weight: 900; margin-bottom: 8px; letter-spacing: -0.5px; }}
.hero p {{ font-size: 14px; opacity: 0.7; font-weight: 500; }}
.container {{
    max-width: 760px; margin: -28px auto 0; padding: 0 20px 40px;
    position: relative; z-index: 1;
}}
.team-card {{
    display: flex; align-items: center; gap: 16px;
    background: #fff; border-radius: 16px; padding: 20px 24px;
    box-shadow: 0 2px 12px rgba(0,0,0,0.06);
    text-decoration: none; color: inherit;
    transition: all 0.2s; border: 1px solid #f0f1f3;
    margin-bottom: 10px;
}}
.team-card:hover {{
    border-color: #0064FF; box-shadow: 0 4px 20px rgba(0,100,255,0.12);
    transform: translateY(-2px);
}}
.team-icon {{
    width: 48px; height: 48px; border-radius: 14px;
    background: #f4f5f7; display: flex; align-items: center; justify-content: center;
    font-size: 24px; flex-shrink: 0;
}}
.team-card:hover .team-icon {{ background: #f0f4ff; }}
.team-info {{ flex: 1; }}
.team-name {{ font-size: 16px; font-weight: 800; color: #1a1a1a; margin-bottom: 4px; }}
.team-regions {{ font-size: 12px; color: #8b95a5; font-weight: 500; }}
.team-arrow {{ font-size: 18px; color: #d0d5dd; font-weight: 300; transition: all 0.2s; }}
.team-card:hover .team-arrow {{ color: #0064FF; transform: translateX(4px); }}
.footer {{ text-align: center; padding: 20px; font-size: 11px; color: #b0b8c4; }}
</style></head><body>
<div class="hero">
    <svg style="height:48px;margin-bottom:16px;" viewBox="0 0 120 32" fill="none" xmlns="http://www.w3.org/2000/svg">
        <text x="0" y="26" font-family="Pretendard,-apple-system,sans-serif" font-size="28" font-weight="900" fill="#FFFFFF" letter-spacing="-1">SOCAR</text>
    </svg>
    <h1>수요/인프라 지도</h1>
    <p>팀(지역)을 선택하세요</p>
</div>
<div class="container">
    {cards}
</div>
<div class="footer">업데이트: {TODAY}</div>
</body></html>'''

    path = os.path.join(OUTPUT_DIR, "index.html")
    with open(path, "w") as f:
        f.write(html)
    print(f"  -> 랜딩 페이지: {path}")


if __name__ == "__main__":
    import sys
    # --team 인자 파싱
    team_id = None
    for i, arg in enumerate(sys.argv):
        if arg == '--team' and i + 1 < len(sys.argv):
            team_id = sys.argv[i + 1]
            break

    teams = list(TEAM_CONFIG.keys()) if team_id == 'all' or team_id is None else [team_id]

    if '--regen' in sys.argv:
        for t in teams:
            print(f"\n{'='*40} {TEAM_CONFIG[t]['name']} {'='*40}")
            regenerate_from_cache(team_id=t)
    elif '--demand' in sys.argv:
        for t in teams:
            print(f"\n{'='*40} {TEAM_CONFIG[t]['name']} {'='*40}")
            update_demand(team_id=t)
    elif '--zone' in sys.argv:
        for t in teams:
            print(f"\n{'='*40} {TEAM_CONFIG[t]['name']} {'='*40}")
            update_zone(team_id=t)
    else:
        for t in teams:
            print(f"\n{'='*40} {TEAM_CONFIG[t]['name']} {'='*40}")
            main(team_id=t)

    # 멀티팀(전체) 모드일 때만 랜딩 페이지 생성
    if len(teams) > 1:
        generate_landing_page()
