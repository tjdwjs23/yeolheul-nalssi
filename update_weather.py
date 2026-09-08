# -*- coding: utf-8 -*-
"""
네이버 날씨 비교 페이지를 스크래핑해 계산된 10일 예보를 data.json으로 저장.
GitHub Actions가 하루 3번(한국시간 6·12·18시) 실행한다.

강수확률 통합 방식 (단순 평균 사용 안 함):
  Base = 중앙값 × 0.8 + 서비스별 신뢰도 가중평균 × 0.2
  + Consensus 특수규칙(A~D)이 Base보다 우선
  + 강수확률과 별개로 예보 일치도(Spread 기반)를 계산
"""
import datetime
import json
import math
import time
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36")

# 조회할 지역 목록 (첫 번째가 기본 지역). 코드는 weather.naver.com/compare/{코드} 주소의 숫자.
# 지역 추가: 네이버 날씨에서 해당 동네 비교 페이지를 열고 주소 끝 코드를 여기 붙이면 된다.
REGIONS = [
    "09110178",  # 서울 종로구 송월동 — 기본
    "02480108",  # 경기 파주시 야당동
    "11275101",  # 인천 서구 검암동
    "02360111",  # 경기 남양주시 별내동
    "02610101",  # 경기 광주시 경안동
    "02135107",  # 경기 성남시 분당구 야탑동
    "02173103",  # 경기 안양시 동안구 평촌동
    "02595102",  # 경기 화성시 병점동
    "02591256",  # 경기 화성시 남양읍
]

# ==================================================
# 강수확률 통합 설정 (Brier Score 기반 가중치 조정을 위해 분리)
# ==================================================

# 예보시간(lead time, 오늘=0)별 서비스 가중치: (최소 lead, 최대 lead(None=무제한), 가중치)
RAIN_WEIGHTS = [
    (0, 2, {"KMA": 0.40, "TWC": 0.30, "WEATHERNEWS": 0.20, "ACCUWEATHER": 0.10}),
    (3, None, {"KMA": 0.30, "TWC": 0.40, "WEATHERNEWS": 0.15, "ACCUWEATHER": 0.15}),
]

MEDIAN_RATIO = 0.8  # Base = Median×0.8 + WeightedMean×0.2

# 최종 확률 → 문구 / 짧은문구: (상한(미포함), 문구, 짧은문구)
RAIN_LABELS = [
    (20, "비 가능성 매우 낮음", "거의 안 옴"),
    (40, "비 가능성 낮음", "가능성 낮음"),
    (60, "비 가능성 있음 / 불확실", "가능성 있음"),
    (80, "비 올 가능성 높음", "가능성 높음"),
    (101, "비 올 가능성 매우 높음", "비 유력"),
]

# 예보 일치도: Spread(최고-최저) 상한(포함) → (코드, 문구)
AGREEMENT_BANDS = [
    (20, "HIGH", "예보 일치도 높음"),
    (40, "MEDIUM", "예보 일치도 보통"),
    (60, "LOW", "예보 일치도 낮음"),
    (100, "VERY_LOW", "예보 크게 엇갈림"),
]

# 기온(℃) 구간별 옷차림표: (하한, 상한(미포함), 외투, 상의)
CLOTHES_BANDS = [
    (28, None, [], ["민소매", "반팔 티셔츠"]),
    (23, 28, [], ["반팔 티셔츠", "얇은 셔츠", "얇은 긴팔 티셔츠"]),
    (20, 23, ["얇은 가디건"], ["긴팔 티셔츠", "셔츠", "블라우스", "후드티"]),
    (17, 20, ["얇은 니트", "얇은 가디건", "얇은 재킷", "바람막이"], ["후드티", "스웨트셔츠(맨투맨)"]),
    (12, 17, ["재킷", "가디건", "야상"], ["스웨트셔츠(맨투맨)", "셔츠", "기모 후드티"]),
    (9, 12, ["재킷", "야상", "점퍼", "트렌치 코트"], []),
    (5, 9, ["(울)코트", "가죽 재킷"], []),
    (None, 5, ["패딩", "두꺼운 코트"], []),
]


def pick_weights(lead):
    for lo, hi, weights in RAIN_WEIGHTS:
        if lead >= lo and (hi is None or lead <= hi):
            return weights
    return RAIN_WEIGHTS[-1][2]


def rain_summary(probs, lead):
    """서비스별 강수확률 dict {서비스: 0~100} + lead(일) → 통합 강수 정보 dict."""
    vals = {k: v for k, v in probs.items() if v is not None}
    if not vals:
        return None
    arr = sorted(vals.values())
    n = len(arr)

    # 중앙값: 짝수 개면 가운데 두 값 평균
    median = (arr[n // 2 - 1] + arr[n // 2]) / 2 if n % 2 == 0 else arr[n // 2]

    # 가중평균 (누락 서비스가 있으면 가중치 재정규화)
    weights = pick_weights(lead)
    wsum = sum(weights.get(k, 0) for k in vals)
    if wsum > 0:
        weighted = sum(v * weights.get(k, 0) for k, v in vals.items()) / wsum
    else:
        weighted = sum(arr) / n

    base = median * MEDIAN_RATIO + weighted * (1 - MEDIAN_RATIO)

    # Consensus 특수규칙 (A > B > C 순으로 우선 적용)
    # 확률값을 조정하는 것은 3곳 이상의 합의가 있을 때뿐이다.
    # 2:2로 갈린 경우는 확률(Base)을 조작하지 않는다 — 불확실성과 확률은 다른 축이므로
    # 확률은 그대로 두고 일치도 쪽에서 "예보 크게 엇갈림"으로 표시한다.
    low20 = sum(1 for v in arr if v <= 20)
    high60 = sum(1 for v in arr if v >= 60)
    high80 = sum(1 for v in arr if v >= 80)
    if low20 >= 3:                              # [A] 3곳 이상 20% 이하 → 최대 20%로 제한
        final = min(base, 20)
    elif high80 >= 3:                           # [B] 3곳 이상 80% 이상 → 최소 80%
        final = max(base, 80)
    elif high60 >= 3:                           # [C] 3곳 이상 60% 이상 → 최소 60% (80으로 올리지 않음)
        final = max(base, 60)
    else:                                       # [D] Base 그대로 (2:2 갈림 포함)
        final = base

    final = max(0, min(100, int(round(final))))

    label = short = None
    for hi, lb, sh in RAIN_LABELS:
        if final < hi:
            label, short = lb, sh
            break

    # 일치도: Spread(최고-최저)는 극단값 하나에 휘둘리는 지표라 그대로 쓰지 않는다.
    # 3개가 20%p 이내로 뭉쳐 있고 하나만 40%p 이상 튀면 "3:1 소수의견"으로 보고,
    # 일치도는 합의된 3개의 Spread로 계산하며 튄 값은 소수의견 경고로 남긴다.
    warning = None
    spread = max(arr) - min(arr)
    if n == 4:
        outlier_key = max(vals, key=lambda k: abs(vals[k] - median))
        trio = sorted(v for k, v in vals.items() if k != outlier_key)
        trio_spread = trio[-1] - trio[0]
        if trio_spread <= 20 and abs(vals[outlier_key] - trio[1]) >= 40:
            spread = trio_spread
            warning = "%s만 %d%% 예보 (소수의견)" % (outlier_key, vals[outlier_key])

    agr_code = agr_text = None
    for hi, code, text in AGREEMENT_BANDS:
        if spread <= hi:
            agr_code, agr_text = code, text
            break

    if n == 4 and high60 == 2 and low20 == 2:   # 2:2 완전 갈림 → 일치도만 최하로
        agr_code, agr_text = "VERY_LOW", "예보 크게 엇갈림"

    out = {"확률": final, "문구": label, "짧은문구": short,
           "일치도": agr_code, "일치도문구": agr_text}
    if warning:
        out["경고"] = warning
    return out


def avg_drop(values):
    """최고·최저 각 1개 제외 후 평균 (온도 통합용)."""
    vals = sorted(v for v in values if v is not None)
    if len(vals) > 1:
        vals = vals[1:]
    if len(vals) > 1:
        vals = vals[:-1]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 1)


def clothes_at(temp):
    if temp is None:
        return [], []
    for lo, hi, outer, top in CLOTHES_BANDS:
        if (lo is None or temp >= lo) and (hi is None or temp < hi):
            return list(outer), list(top)
    return [], []


# 옷차림 기준온도: 일교차 대비 상승 비율
# 오전 9시 비율은 고정값이 아니라 그날의 일출시각으로 계산한다:
#   AM_RATIO = (9시 - 일출) / (15시 - 일출)   ← 15시 = 대략적인 일최고기온 도달 시각(thermal lag)
# 일최저는 일출 무렵에 나타나므로, 9시 기온의 위치는 "일출 후 얼마나 지났느냐"가 결정한다.
# 한여름(일출 5시대) ≈ 0.39, 한겨울(일출 7시대 후반) ≈ 0.20으로 자연스럽게 계절이 반영된다.
AM_HOUR = 9        # 오전 기준 시각
PEAK_HOUR = 15     # 일최고기온 도달 가정 시각
AM_RATIO_MIN = 0.20
AM_RATIO_MAX = 0.40
PM_RATIO = 0.85    # 오후 1시: 최고기온에 상당히 근접 (고정)


def sunrise_hour(lat, lon, date, tz=9):
    """NOAA 근사식으로 일출 시각(현지시간, 시 단위 float)을 계산. 오차 수 분 이내."""
    n = date.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    lat_r = math.radians(lat)
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(lat_r) * math.cos(decl))
              - math.tan(lat_r) * math.tan(decl))
    cos_ha = max(-1.0, min(1.0, cos_ha))
    ha = math.degrees(math.acos(cos_ha))
    minutes_utc = 720 - 4 * (lon + ha) - eqtime
    return (minutes_utc / 60 + tz) % 24


def am_ratio_for(lat, lon, date):
    """일출 기반 오전 비율: (9시 - 일출) / (15시 - 일출), 0.20~0.40으로 제한."""
    rise = sunrise_hour(lat, lon, date)
    ratio = (AM_HOUR - rise) / (PEAK_HOUR - rise)
    return max(AM_RATIO_MIN, min(AM_RATIO_MAX, ratio)), rise


def ref_temp(tmin, tmax, ratio):
    """기준온도 추정: 최저 + 일교차 × 비율.
    표시는 0.5 단위 스냅: 소수 첫째 자리가 5 미만이면 내림, 5면 .5 유지, 5 초과면 올림."""
    if tmin is None or tmax is None:
        return tmin
    scaled = int(round((tmin + (tmax - tmin) * ratio) * 10))
    whole, digit = divmod(scaled, 10)
    if digit < 5:
        return whole
    if digit == 5:
        return whole + 0.5
    return whole + 1


def scrape_region(region_code, kst_today):
    """지역코드 하나를 스크래핑해 계산된 예보 dict를 반환."""
    req = urllib.request.Request("https://weather.naver.com/compare/" + region_code,
                                 headers={"User-Agent": UA})
    html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8")
    marker = "var blockApiResult = "
    idx = html.find(marker)
    if idx < 0:
        raise SystemExit("blockApiResult 없음 (페이지 구조 변경?)")
    data, _ = json.JSONDecoder().raw_decode(html, idx + len(marker))
    cr = data["results"]["choiceResult"]
    region = cr["selectedRegion~~1"]["naverRegion"]
    region_name = " ".join(filter(None, [region.get("lareaNm"), region.get("mareaNm"), region.get("sareaNm")]))
    lat = region.get("latitude") or 37.5665   # 좌표가 없으면 서울 시청 기준
    lon = region.get("longitude") or 126.978
    provider_map = cr["compareWeeklyFcast~~1"]["domesticWeeklyListMap"]

    by_date = {}
    for provider, plist in provider_map.items():
        for d in plist:
            e = by_date.setdefault(d["aplYmd"], {"day": d.get("dayString"),
                                                 "min": [], "max": [], "am": {}, "pm": {}})
            e["min"].append(d.get("minTmpr"))
            e["max"].append(d.get("maxTmpr"))
            e["am"][provider] = d.get("amRainProb")
            e["pm"][provider] = d.get("pmRainProb")

    days = []
    for ymd in sorted(by_date):
        e = by_date[ymd]
        day_date = datetime.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:]))
        lead = max((day_date - kst_today).days, 0)
        tmin = avg_drop(e["min"])
        tmax = avg_drop(e["max"])
        am_ratio, rise = am_ratio_for(lat, lon, day_date)
        t_am = ref_temp(tmin, tmax, am_ratio)
        t_pm = ref_temp(tmin, tmax, PM_RATIO)
        am_outer, am_top = clothes_at(t_am)
        pm_outer, pm_top = clothes_at(t_pm)
        days.append({
            "날짜": "%s-%s-%s" % (ymd[:4], ymd[4:6], ymd[6:]),
            "요일": e["day"],
            "일출": "%02d:%02d" % (int(rise), int(rise % 1 * 60)),
            "최저온도": tmin, "최고온도": tmax,
            "오전": {"기준온도": t_am, "강수": rain_summary(e["am"], lead), "외투": am_outer, "상의": am_top},
            "오후": {"기준온도": t_pm, "강수": rain_summary(e["pm"], lead), "외투": pm_outer, "상의": pm_top},
        })

    return {"지역코드": region_code, "지역명": region_name,
            "제공사": sorted(provider_map.keys()), "일자별": days}


kst_now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
kst_today = kst_now.date()

regions = {}
for code in REGIONS:
    regions[code] = scrape_region(code, kst_today)
    print("수집:", regions[code]["지역명"], len(regions[code]["일자별"]), "일치")
    time.sleep(1)  # 네이버에 연속 요청 간 간격

result = {
    "기본지역": REGIONS[0],
    "지역들": regions,
    "업데이트": kst_now.strftime("%Y-%m-%d %H:%M") + " KST",
}
with open("data.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=1)
print("저장됨:", len(regions), "개 지역")
