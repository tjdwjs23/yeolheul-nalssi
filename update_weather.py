# -*- coding: utf-8 -*-
"""
네이버 날씨 비교 페이지를 스크래핑해 계산된 10일 예보를 data.json으로 저장.
GitHub Actions가 하루 3번(한국시간 6·12·18시) 실행한다.
"""
import json
import time
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36")
REGION = "02135109"  # 경기도 성남시 분당구 삼평동

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


def avg_drop(values):
    """최고·최저 각 1개 제외 후 평균 (4개 값이면 중앙값과 동일)."""
    vals = sorted(v for v in values if v is not None)
    if len(vals) > 1:
        vals = vals[1:]
    if len(vals) > 1:
        vals = vals[:-1]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 1)


def rain_label(values):
    m = avg_drop(values)
    if m is None:
        return None
    if m < 30:
        return "안옴"
    if m < 60:
        return "가능성 낮음"
    if m < 70:
        return "비올 가능성 있음"
    if m < 80:
        return "비올 가능성 높음"
    return "비옴"


def clothes_at(temp):
    if temp is None:
        return [], []
    for lo, hi, outer, top in CLOTHES_BANDS:
        if (lo is None or temp >= lo) and (hi is None or temp < hi):
            return list(outer), list(top)
    return [], []


def am_temp(tmin, tmax):
    """오전 9시 기준온도 추정: 최저 + 일교차의 45%."""
    if tmin is None or tmax is None:
        return tmin
    return round(tmin + (tmax - tmin) * 0.45, 1)


req = urllib.request.Request("https://weather.naver.com/compare/" + REGION,
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
provider_map = cr["compareWeeklyFcast~~1"]["domesticWeeklyListMap"]

by_date = {}
for provider, plist in provider_map.items():
    for d in plist:
        e = by_date.setdefault(d["aplYmd"], {"day": d.get("dayString"),
                                             "min": [], "max": [], "am": [], "pm": []})
        e["min"].append(d.get("minTmpr"))
        e["max"].append(d.get("maxTmpr"))
        e["am"].append(d.get("amRainProb"))
        e["pm"].append(d.get("pmRainProb"))

days = []
for ymd in sorted(by_date):
    e = by_date[ymd]
    tmin = avg_drop(e["min"])
    tmax = avg_drop(e["max"])
    t_am = am_temp(tmin, tmax)
    t_pm = tmax
    am_outer, am_top = clothes_at(t_am)
    pm_outer, pm_top = clothes_at(t_pm)
    days.append({
        "날짜": "%s-%s-%s" % (ymd[:4], ymd[4:6], ymd[6:]),
        "요일": e["day"],
        "최저온도": tmin, "최고온도": tmax,
        "오전": {"기준온도": t_am, "강수": rain_label(e["am"]), "외투": am_outer, "상의": am_top},
        "오후": {"기준온도": t_pm, "강수": rain_label(e["pm"]), "외투": pm_outer, "상의": pm_top},
    })

kst = time.gmtime(time.time() + 9 * 3600)
result = {
    "지역코드": REGION, "지역명": region_name,
    "제공사": sorted(provider_map.keys()), "일자별": days,
    "업데이트": time.strftime("%Y-%m-%d %H:%M", kst) + " KST",
}
with open("data.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=1)
print("저장됨:", region_name, len(days), "일치")
