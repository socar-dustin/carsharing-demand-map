#!/usr/bin/env python3
"""
전기차 충전기 상태 수집 (30분마다 cron 실행)
getChargerInfo로 전체 충전기 상태 조회 → 충전소별 이용률 정확 산출
"""
import json, os, urllib.request, ssl, time
from datetime import datetime

API_KEY = '5145469085c4a37aebedd3f2859b7c4616180a6b57ce5a999e2afa96ed472e9a'
ZCODES = ['41', '51']  # 경기, 강원
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.cache', 'ev_status_log')
os.makedirs(LOG_DIR, exist_ok=True)

# stat 코드: 1=통신이상, 2=충전대기, 3=충전중, 4=운영중지, 5=점검중, 9=상태미확인
STAT_CHARGING = '3'
STAT_AVAILABLE = '2'

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def collect():
    now = datetime.now()
    ts = now.strftime('%Y%m%d%H%M')
    hour = now.hour
    weekday = now.weekday()  # 0=월 ~ 6=일

    all_items = []
    for zcode in ZCODES:
        page = 1
        while True:
            url = (f"http://apis.data.go.kr/B552584/EvCharger/getChargerInfo"
                   f"?serviceKey={API_KEY}&numOfRows=9999&pageNo={page}"
                   f"&dataType=JSON&zcode={zcode}")
            req = urllib.request.Request(url, headers={'User-Agent': 'socar-ev-collector/1.0'})
            try:
                with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
                    data = json.loads(resp.read())
                items_wrapper = data.get('items', {})
                items = items_wrapper.get('item', []) if isinstance(items_wrapper, dict) else items_wrapper
                if not items:
                    break
                all_items.extend(items)
                total = int(data.get('totalCount', 0))
                if page * 9999 >= total:
                    break
                page += 1
            except Exception as e:
                print(f"[{ts}] API error (zcode={zcode}, page={page}): {e}")
                break
            time.sleep(0.5)

    # 충전소별 전체/충전중 충전기 수 집계
    station_stats = {}  # {statId: {'total': N, 'charging': N, 'available': N}}
    for item in all_items:
        sid = item.get('statId', '')
        if not sid:
            continue
        stat = str(item.get('stat', ''))
        if sid not in station_stats:
            station_stats[sid] = {'total': 0, 'charging': 0, 'available': 0}
        station_stats[sid]['total'] += 1
        if stat == STAT_CHARGING:
            station_stats[sid]['charging'] += 1
        elif stat == STAT_AVAILABLE:
            station_stats[sid]['available'] += 1

    # 로그 저장
    date_str = now.strftime('%Y-%m-%d')
    log_file = os.path.join(LOG_DIR, f'{date_str}.jsonl')
    total_charging = sum(s['charging'] for s in station_stats.values())
    total_available = sum(s['available'] for s in station_stats.values())
    record = {
        'ts': ts,
        'hour': hour,
        'weekday': weekday,
        'total_chargers': len(all_items),
        'charging_count': total_charging,
        'available_count': total_available,
        'stations': station_stats,
    }
    with open(log_file, 'a') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')

    print(f"[{ts}] 수집완료: {len(all_items)}건 ({len(station_stats)}개소), "
          f"충전중 {total_charging}, 대기중 {total_available} → {log_file}")


if __name__ == '__main__':
    collect()
