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
import re
import time
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36")

# 조회할 지역 목록 (첫 번째가 기본 지역). 코드는 weather.naver.com/compare/{코드} 주소의 숫자.
# 지역 추가: 네이버 날씨에서 해당 동네 비교 페이지를 열고 주소 끝 코드를 여기 붙이면 된다.
REGIONS = [
    "09650101",  # 서울 서초구 방배동 — 기본
    "09140125",  # 서울 중구 충무로2가
    "09740108",  # 서울 강동구 성내동
    "09560113",  # 서울 영등포구 당산동3가
    "11237104",  # 인천 부평구 청천동
    "02111132",  # 경기 수원시 장안구 율전동
    "02480101",  # 경기 파주시 금촌동
    "02610112",  # 경기 광주시 역동
    "02360103",  # 경기 남양주시 금곡동
    "11185106",  # 인천 연수구 송도동 — 트리플스트리트·현대아울렛
    "02135105",  # 경기 성남시 분당구 서현동 — 서현역 로데오
    "02285104",  # 경기 고양시 일산동구 장항동 — 라페스타·웨스턴돔
    "02220113",  # 경기 평택시 평택동 — 평택역 로데오
    "09350105",  # 서울 노원구 상계동 — 노원역 문화의거리
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

# ==================================================
# 평년값: 기상청 1991~2020 일별 평년값, 서울(108) 지점
# 갱신할 때마다 기상자료개방포털(data.kma.go.kr)에서 예보 기간(열흘)만 조회한다.
# 조회에 실패하면 평년 비교("평년비교")는 생략된다(페이지에서 카드 숨김).
# ==================================================
NORMALS_URL = "https://data.kma.go.kr/climate/average30Years/selectAverage30YearsList.do"
NORMALS_BASE = ("pgmNo=113&menuNo=652&serviceSe=F00101&selectType=1&mddlClssCd=SFC01"
                "&schStnId=108&startYear=2021&schGubun=3")


def _fetch_normals_span(sm, sd, em, ed):
    """(시작월/일 ~ 끝월/일) 구간의 일별 평년값을 조회해 {(월,일): (최저,최고)} 반환."""
    params = NORMALS_BASE + "&startMonth=%d&startDay=%02d&endMonth=%d&endDay=%02d" % (sm, sd, em, ed)
    req = urllib.request.Request(NORMALS_URL, data=params.encode(),
                                 headers={"User-Agent": UA})
    html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    table = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [re.sub(r"<[^>]+>|\s+", " ", c).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        # 형식: [MM-DD, 평균, 최고, 최저, 강수량] (하루짜리 조회는 지점명 열이 끼므로 길이로 구분)
        if len(cells) >= 5 and re.match(r"\d\d-\d\d$", cells[0]):
            off = 1 if not re.match(r"-?\d", cells[1]) else 0  # 지점명 열이 있으면 건너뜀
            m, d = int(cells[0][:2]), int(cells[0][3:])
            table[(m, d)] = (float(cells[3 + off]), float(cells[2 + off]))  # (최저, 최고)
    return table


def fetch_daily_normals(start_date, end_date):
    """예보 기간의 일별 평년값 조회. 연말→연초로 걸치면 두 번 나눠 합친다."""
    if (start_date.month, start_date.day) <= (end_date.month, end_date.day):
        table = _fetch_normals_span(start_date.month, start_date.day,
                                    end_date.month, end_date.day)
    else:
        table = _fetch_normals_span(start_date.month, start_date.day, 12, 31)
        table.update(_fetch_normals_span(1, 1, end_date.month, end_date.day))
    # 서버가 구간 요청에도 1년치를 돌려주는 경우가 있어, 필요한 날짜만 골라낸다
    wanted = set()
    cur = start_date
    while cur <= end_date:
        wanted.add((cur.month, cur.day))
        cur += datetime.timedelta(days=1)
    table = {k: v for k, v in table.items() if k in wanted}
    expected = (end_date - start_date).days + 1
    if len(table) < expected - 1:  # 2/29 등 한두 개 빠지는 건 허용
        raise ValueError("일별 평년값 %d/%d일치만 수신" % (len(table), expected))
    return table


def normal_temp(normals, date, kind):
    """해당 날짜의 공식 일별 평년값("min"=일최저, "max"=일최고)."""
    pair = normals.get((date.month, date.day))
    if pair is None:
        return None
    return pair[0] if kind == "min" else pair[1]


# 계절감 판정에서 월이 하는 역할: 계절을 결정하는 게 아니라 "연중 기온 방향"만 구분한다.
# 같은 평균 12°/최저 7°라도 4월(상승기)이면 봄, 11월(하강기)이면 가을.
# (기온 90% + 달력은 방향성 10%. 튜닝 가능하도록 상수로 분리)
WARMING_FIRST, WARMING_LAST = 2, 7   # 2~7월 = warming phase(봄 계열), 8~1월 = cooling phase(가을 계열)
EARLY_SUMMER_LAST = 7                # 초여름은 7월까지, 8월부터는 늦여름
EARLY_WINTER_FROM = 7                # 7월 이후(실질적으로 11~12월)는 초겨울, 1~2월은 늦겨울


def season_feel(tmin, tmax, month):
    """그날 기온이 어느 계절처럼 느껴지는지(계절감)를 판정한다.
    "지금이 무슨 계절인가"가 아니다 — 기상청의 공식 계절 구분은 9일 이동평균과
    지속성 조건을 쓰지만, 여기서는 하루치 스냅샷이라 날마다 달라질 수 있고
    그래서 UI에서도 "늦여름 날씨"처럼 표기한다. 옷차림 계산에는 쓰지 않는 보조 정보.

    일평균기온은 (최저+최고)/2로 근사 (최저·최고가 이미 4사 통합값이라 강건함).
    기준표(위키백과 기후학적 계절 세분류와 동일):
    - 한여름: 평균 25 이상 + 최고 30 이상 / 초·늦여름: 평균 [20,25) + 최고 25 이상
    - 늦봄·초가을: 평균 [15,20) + 최저 10 이상 / 봄·가을: 평균 [10,15) + 최저 5 이상
    - 초봄·늦가을: 평균 [5,10) + 최저 0 이상 / 초·늦겨울: 평균 5 미만 + 최저 0 이하
    - 한겨울: 평균 0 이하 + 최저 -5 이하
    구현 규칙:
    - 경계값은 반열림 구간 [하한, 상한)으로 통일 (평균기온 내림차순 캐스케이드).
    - "초가을: 최고 25 이하"는 전역 조건이 아니라 cooling phase에서 여름→가을로
      넘어가는 경계조건으로만 쓴다 (평균 20~25인데 최고가 25에 못 미치는 경우).
      캐스케이드 구조상 한겨울 날씨가 초가을로 새는 일은 없다.
    - 부가 조건(최저/최고) 미달 시 해당 방향 사다리에서 한 칸 서늘한 쪽으로 내린다.
      (warming: 늦겨울→초봄→봄→늦봄→초여름→한여름 / cooling: 그 역방향)
      내려간 라벨은 "엄밀한 정의 충족"이 아니라 근사 표시다."""
    if tmin is None or tmax is None:
        return None
    tavg = (tmin + tmax) / 2
    spring = WARMING_FIRST <= month <= WARMING_LAST
    early_summer = month <= EARLY_SUMMER_LAST
    early_winter = month >= EARLY_WINTER_FROM
    if tavg >= 25:
        return "한여름" if tmax >= 30 else ("초여름" if early_summer else "늦여름")
    if tavg >= 20:
        if tmax >= 25:
            return "초여름" if early_summer else "늦여름"
        return "늦봄" if spring else "초가을"
    if tavg >= 15:
        if tmin >= 10:
            return "늦봄" if spring else "초가을"
        return "봄" if spring else "가을"
    if tavg >= 10:
        if tmin >= 5:
            return "봄" if spring else "가을"
        return "초봄" if spring else "늦가을"
    if tavg >= 5:
        if tmin >= 0:
            return "초봄" if spring else "늦가을"
        return "초겨울" if early_winter else "늦겨울"
    if tavg > 0 or tmin > -5:
        return "초겨울" if early_winter else "늦겨울"
    return "한겨울"


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
    # 3개가 20%p 이내로 뭉쳐 있고 하나만 40%p 이상 튀면 "3:1 소수의견"으로 보고
    # 일치도는 합의된 3개의 Spread로 계산한다. (소수의견 자체는 별도 표시하지 않음)
    spread = max(arr) - min(arr)
    if n == 4:
        outlier_key = max(vals, key=lambda k: abs(vals[k] - median))
        trio = sorted(v for k, v in vals.items() if k != outlier_key)
        trio_spread = trio[-1] - trio[0]
        if trio_spread <= 20 and abs(vals[outlier_key] - trio[1]) >= 40:
            spread = trio_spread

    agr_code = agr_text = None
    for hi, code, text in AGREEMENT_BANDS:
        if spread <= hi:
            agr_code, agr_text = code, text
            break

    if n == 4 and high60 == 2 and low20 == 2:   # 2:2 완전 갈림 → 일치도만 최하로
        agr_code, agr_text = "VERY_LOW", "예보 크게 엇갈림"

    return {"확률": final, "문구": label, "짧은문구": short,
            "일치도": agr_code, "일치도문구": agr_text}


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


# 옷차림 기준온도(오전 9시 / 오후 1시)는 같은 페이지의 시간별 예보에 있는
# 실제 9시·13시 온도로 계산한다:
#   기준일 = 그 시각이 아직 안 지났으면 오늘, 지났으면 다음날
#   오전 차이 = 기준일 9시 온도(4사 최고·최저 제외 평균) − 기준일 통합 최저기온
#   오후 차이 = 기준일 13시 온도(〃) − 기준일 통합 최고기온
#   각 날짜의 기준온도 = 그날 최저 + 오전 차이 / 그날 최고 + 오후 차이
# (시간별 예보는 약 2일치뿐이라, 기준일에서 뽑은 '차이'를 10일 전체에 적용)
AM_HOUR = 9        # 오전 기준 시각
PM_HOUR = 13       # 오후 기준 시각

# --- 아래는 시간별 예보를 구하지 못했을 때의 예비(일출 기반) 공식 ---
#   AM_RATIO = (9시 - 일출) / (15시 - 일출)   ← 15시 = 대략적인 일최고기온 도달 시각(thermal lag)
# 한여름(일출 5시대) ≈ 0.39, 한겨울(일출 7시대 후반) ≈ 0.20으로 계절이 자연스럽게 반영된다.
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


def snap_half(value):
    """표시용 0.5 단위 스냅: 소수 첫째 자리가 5 미만이면 내림, 5면 .5 유지, 5 초과면 올림."""
    scaled = int(round(value * 10))
    whole, digit = divmod(scaled, 10)
    if digit < 5:
        return whole
    if digit == 5:
        return whole + 0.5
    return whole + 1


def ref_temp(tmin, tmax, ratio):
    """(예비 공식) 기준온도 추정: 최저 + 일교차 × 비율."""
    if tmin is None or tmax is None:
        return tmin
    return snap_half(tmin + (tmax - tmin) * ratio)


def scrape_region(region_code, kst_now, normals):
    kst_today = kst_now.date()
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
    hourly_map = (cr.get("compareHourlyFcast~~1") or {}).get("domesticHourlyListMap") or {}

    def hourly_united(ymd, hour):
        """해당 날짜·시각의 4사 시간별 온도를 최고·최저 제외 평균으로 통합."""
        vals = []
        for plist in hourly_map.values():
            for h in plist:
                try:
                    if h.get("aplYmd") == ymd and int(h.get("aplTm")) == hour:
                        vals.append(h.get("tmpr"))
                        break
                except (TypeError, ValueError):
                    continue
        return avg_drop(vals)

    by_date = {}
    for provider, plist in provider_map.items():
        for d in plist:
            e = by_date.setdefault(d["aplYmd"], {"day": d.get("dayString"),
                                                 "min": [], "max": [], "am": {}, "pm": {}})
            e["min"].append(d.get("minTmpr"))
            e["max"].append(d.get("maxTmpr"))
            e["am"][provider] = d.get("amRainProb")
            e["pm"][provider] = d.get("pmRainProb")

    # 날짜별 통합 최저/최고를 먼저 계산 (기준일 차이 계산에 필요)
    temps = {ymd: (avg_drop(e["min"]), avg_drop(e["max"])) for ymd, e in by_date.items()}

    def anchor_delta(hour, use_max):
        """실제 시간별 예보로 (기준시각 온도 − 기준일 최저/최고) 차이를 구한다.
        기준일: 오늘 그 시각이 아직 안 지났으면 오늘, 지났으면 다음날."""
        basis = kst_today if kst_now.hour < hour else kst_today + datetime.timedelta(days=1)
        ymd = basis.strftime("%Y%m%d")
        t_hour = hourly_united(ymd, hour)
        tmin_b, tmax_b = temps.get(ymd, (None, None))
        ref = tmax_b if use_max else tmin_b
        if t_hour is None or ref is None:
            return None
        return t_hour - ref

    delta_am = anchor_delta(AM_HOUR, use_max=False)  # 기준일 9시 온도 − 기준일 최저
    delta_pm = anchor_delta(PM_HOUR, use_max=True)   # 기준일 13시 온도 − 기준일 최고

    days = []
    for ymd in sorted(by_date):
        e = by_date[ymd]
        day_date = datetime.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:]))
        lead = max((day_date - kst_today).days, 0)
        tmin, tmax = temps[ymd]
        rise = sunrise_hour(lat, lon, day_date)
        # 실제 시간별 예보에서 뽑은 차이를 우선 적용, 없으면 일출 기반 예비 공식
        if delta_am is not None and tmin is not None:
            t_am = snap_half(tmin + delta_am)
        else:
            t_am = ref_temp(tmin, tmax, am_ratio_for(lat, lon, day_date)[0])
        if delta_pm is not None and tmax is not None:
            t_pm = snap_half(tmax + delta_pm)
        else:
            t_pm = ref_temp(tmin, tmax, PM_RATIO)
        am_outer, am_top = clothes_at(t_am)
        pm_outer, pm_top = clothes_at(t_pm)
        days.append({
            "날짜": "%s-%s-%s" % (ymd[:4], ymd[4:6], ymd[6:]),
            "요일": e["day"],
            "계절": season_feel(tmin, tmax, day_date.month),
            "일출": "%02d:%02d" % (int(rise), int(rise % 1 * 60)),
            "최저온도": tmin, "최고온도": tmax,
            "오전": {"기준온도": t_am, "강수": rain_summary(e["am"], lead), "외투": am_outer, "상의": am_top},
            "오후": {"기준온도": t_pm, "강수": rain_summary(e["pm"], lead), "외투": pm_outer, "상의": pm_top},
        })

    # 열흘 전체를 평년(1991~2020)과 비교한 평균 편차 (전 지역 서울 관측소 기준)
    compare = None
    if normals:
        diff_min, diff_max = [], []
        for d in days:
            y, m, dd = (int(x) for x in d["날짜"].split("-"))
            date = datetime.date(y, m, dd)
            n_min = normal_temp(normals, date, "min")
            n_max = normal_temp(normals, date, "max")
            if d["최저온도"] is not None and n_min is not None:
                diff_min.append(d["최저온도"] - n_min)
            if d["최고온도"] is not None and n_max is not None:
                diff_max.append(d["최고온도"] - n_max)
        if diff_min and diff_max:
            compare = {"최저차": round(sum(diff_min) / len(diff_min), 1),
                       "최고차": round(sum(diff_max) / len(diff_max), 1),
                       "관측소": "서울"}

    return {"지역코드": region_code, "지역명": region_name,
            "제공사": sorted(provider_map.keys()), "일자별": days,
            "평년비교": compare}


kst_now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
kst_today = kst_now.date()

try:
    daily_normals = fetch_daily_normals(kst_today - datetime.timedelta(days=1),
                                        kst_today + datetime.timedelta(days=10))
    print("평년값 조회 성공 (기상자료개방포털, %d일치)" % len(daily_normals))
except Exception as exc:
    daily_normals = None
    print("평년값 조회 실패, 평년 비교 생략:", exc)

regions = {}
for code in REGIONS:
    regions[code] = scrape_region(code, kst_now, daily_normals)
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
