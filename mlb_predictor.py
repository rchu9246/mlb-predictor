#!/usr/bin/env python3
"""
MLB 每日勝率預測系統
======================
基於 Log5 公式 + 主場優勢 + 先發投手戰力 + 傷兵嚴重度 的量化預測模型。

最簡單的每日用法(全自動):
    python mlb_predictor.py --live --html

    這會自動:
    1. 從 MLB 官方 API 抓取最新戰績
    2. 從 MLB 官方 API 抓取明天的對戰組合與先發投手
    3. 計算勝率並產生一份好看的網頁報告
    4. 自動用瀏覽器打開報告

資料來源(可自行更新,見 data/ 資料夾):
    data/standings.json       各隊季賽勝敗戰績(--live 時會自動覆蓋,失敗才退回本檔)
    data/pitcher_ratings.json 投手戰力評分表(1~10分,新投手預設6分,可自行調整)
    data/injury_scores.json   各隊傷兵嚴重度分數(目前無公開API,需手動更新)
    data/matchups.json        對戰表(--live 時會自動覆蓋,失敗才退回本檔)
"""

import json
import argparse
import os
import sys
import webbrowser
import urllib.request
from datetime import date, timedelta

# ---------------------------------------------------------------------------
# 路徑設定
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

STANDINGS_FILE = os.path.join(DATA_DIR, "standings.json")
PITCHER_FILE = os.path.join(DATA_DIR, "pitcher_ratings.json")
INJURY_FILE = os.path.join(DATA_DIR, "injury_scores.json")
MATCHUPS_FILE = os.path.join(DATA_DIR, "matchups.json")
HISTORY_DIR = os.path.join(DATA_DIR, "history")
DEFAULT_HTML_OUTPUT = os.path.join(BASE_DIR, "report.html")
DEFAULT_VERIFY_HTML_OUTPUT = os.path.join(BASE_DIR, "verify_report.html")

# ---------------------------------------------------------------------------
# 模型權重(可依需求調整)
# ---------------------------------------------------------------------------
HOME_FIELD_ADVANTAGE = 0.54   # 聯盟平均主場勝率基準
PITCHER_WEIGHT = 0.018        # 每1分投手戰力差 → 勝率變動
INJURY_WEIGHT = 0.0009        # 每1分傷兵嚴重度差 → 勝率變動
DEFAULT_PITCHER_SCORE = 6.0   # 未知投手預設戰力(聯盟平均)
DEFAULT_INJURY_SCORE = 60     # 未知球隊預設傷兵分(聯盟平均)

TEAM_NAME_ZH = {
    "TB": "光芒", "NYY": "洋基", "BOS": "紅襪", "TOR": "藍鳥", "BAL": "金鶯",
    "CLE": "守護者", "CWS": "白襪", "DET": "老虎", "MIN": "雙城", "KC": "皇家",
    "TEX": "遊騎兵", "HOU": "太空人", "SEA": "水手", "ATH": "運動家", "LAA": "天使",
    "MIL": "釀酒人", "CHC": "小熊", "PIT": "海盜", "STL": "紅雀", "CIN": "紅人",
    "ATL": "勇士", "PHI": "費城人", "MIA": "馬林魚", "WSH": "國民", "NYM": "大都會",
    "LAD": "道奇", "SD": "教士", "AZ": "響尾蛇", "SF": "巨人", "COL": "落磯",
}


def zh(abbr):
    return TEAM_NAME_ZH.get(abbr, abbr)


# ---------------------------------------------------------------------------
# 資料載入
# ---------------------------------------------------------------------------
def load_json(path):
    if not os.path.exists(path):
        print(f"[錯誤] 找不到資料檔案: {path}")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_team_abbr_map():
    """MLB Stats API 的 standings 端點不含縮寫,需另外用 teams 端點對照 id -> 縮寫"""
    url = "https://statsapi.mlb.com/api/v1/teams?sportId=1"
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    id_to_abbr = {}
    for team in data.get("teams", []):
        team_id = team.get("id")
        abbr = team.get("abbreviation", "")
        if team_id and abbr:
            id_to_abbr[team_id] = abbr
    return id_to_abbr


def _normalize_abbr(abbr, standings_style=False):
    """把 MLB API 少數不一致的縮寫(如運動家隊)統一成本程式慣用代碼"""
    mapping = {"OAK": "ATH", "AT": "ATH", "ARI": "AZ"}
    return mapping.get(abbr, abbr)


def fetch_live_standings(id_to_abbr):
    """從 MLB 官方 Stats API 抓取即時戰績,失敗則退回本地檔案"""
    url = "https://statsapi.mlb.com/api/v1/standings?leagueId=103,104"
    print(f"正在抓取即時戰績...  ({url})")
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"[警告] 無法連線抓取戰績 ({e}),改用本地 standings.json")
        return load_json(STANDINGS_FILE)

    standings = {}
    for record in data.get("records", []):
        for team_record in record.get("teamRecords", []):
            team_id = team_record["team"].get("id")
            abbr = _normalize_abbr(id_to_abbr.get(team_id, ""))
            wins = team_record.get("wins", 0)
            losses = team_record.get("losses", 0)
            if abbr:
                standings[abbr] = [wins, losses]

    if not standings:
        print("[警告] 即時戰績抓取結果為空,改用本地 standings.json")
        return load_json(STANDINGS_FILE)
    return standings


def fetch_live_matchups(id_to_abbr, target_date):
    """
    從 MLB 官方 Stats API 抓取指定日期的對戰表與先發投手,失敗則退回本地檔案。
    target_date: "YYYY-MM-DD" 字串
    """
    url = (
        "https://statsapi.mlb.com/api/v1/schedule"
        f"?sportId=1&date={target_date}&hydrate=probablePitcher,team"
    )
    print(f"正在抓取 {target_date} 對戰表與先發投手...  ({url})")
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"[警告] 無法連線抓取對戰表 ({e}),改用本地 matchups.json")
        return load_json(MATCHUPS_FILE)

    matchups = []
    seen = set()
    for d in data.get("dates", []):
        for game in d.get("games", []):
            teams = game.get("teams", {})
            home = teams.get("home", {})
            away = teams.get("away", {})
            home_id = home.get("team", {}).get("id")
            away_id = away.get("team", {}).get("id")
            home_abbr = _normalize_abbr(id_to_abbr.get(home_id, ""))
            away_abbr = _normalize_abbr(id_to_abbr.get(away_id, ""))
            if not home_abbr or not away_abbr:
                continue
            home_pitcher = home.get("probablePitcher", {}).get("fullName", "TBD")
            away_pitcher = away.get("probablePitcher", {}).get("fullName", "TBD")
            home_pitcher_id = home.get("probablePitcher", {}).get("id")
            away_pitcher_id = away.get("probablePitcher", {}).get("id")

            # 同一天若有雙重賽(doubleheader),同組對戰會出現兩次,都保留
            key = (home_abbr, away_abbr, home_pitcher, away_pitcher, game.get("gamePk"))
            if key in seen:
                continue
            seen.add(key)
            matchups.append({
                "home": home_abbr,
                "away": away_abbr,
                "home_pitcher": home_pitcher,
                "away_pitcher": away_pitcher,
                "home_pitcher_id": home_pitcher_id,
                "away_pitcher_id": away_pitcher_id,
            })

    if not matchups:
        print(f"[警告] {target_date} 沒有抓到任何比賽,改用本地 matchups.json")
        return load_json(MATCHUPS_FILE)
    return matchups


# ---------------------------------------------------------------------------
# 即時投手戰力(用 MLB API 原始數據自算 FIP,取代主觀評分)
# ---------------------------------------------------------------------------
FIP_CONSTANT = 3.10  # 近年聯盟平均FIP常數的近似值,每季實際數字略有變動


def parse_innings_pitched(ip_str):
    """MLB 用 142.1 代表142又1/3局、142.2代表142又2/3局(不是十進位小數)"""
    try:
        s = str(ip_str)
        if "." in s:
            whole_str, frac_str = s.split(".", 1)
        else:
            whole_str, frac_str = s, "0"
        whole = float(whole_str) if whole_str else 0.0
        frac_map = {"0": 0.0, "1": 1 / 3, "2": 2 / 3}
        return whole + frac_map.get(frac_str, 0.0)
    except Exception:
        return 0.0


def fip_to_score(fip):
    """把FIP轉成1~10分戰力評分。以聯盟平均FIP(約4.10)對應6分,每差1分FIP約差1.8分戰力"""
    LEAGUE_AVG_FIP = 4.10
    score = 6.0 - (fip - LEAGUE_AVG_FIP) * 1.8
    return max(1.0, min(10.0, score))


def fetch_pitcher_live_score(person_id, season):
    """
    用 MLB Stats API 抓該投手本季原始數據(局數/三振/保送/被全壘打),
    自行換算FIP再轉成1~10分戰力分數。
    局數太少(<15局,樣本不可靠)回傳 None,由呼叫端改用備援分數。
    """
    url = (
        f"https://statsapi.mlb.com/api/v1/people/{person_id}/stats"
        f"?stats=season&group=pitching&season={season}"
    )
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read().decode())

    for stat_group in data.get("stats", []):
        splits = stat_group.get("splits", [])
        if not splits:
            continue
        stat = splits[0].get("stat", {})
        ip = parse_innings_pitched(stat.get("inningsPitched", "0.0"))
        if ip < 15:
            return None
        hr = stat.get("homeRuns", 0)
        bb = stat.get("baseOnBalls", 0)
        k = stat.get("strikeOuts", 0)
        fip = (13 * hr + 3 * bb - 2 * k) / ip + FIP_CONSTANT
        return fip_to_score(fip)
    return None


def fetch_pitcher_scores_for_matchups(matchups, season, pitcher_ratings):
    """
    幫每場比賽補上 home_pitcher_score / away_pitcher_score。
    優先序: MLB API即時算出的FIP戰力分數 → data/pitcher_ratings.json 手動分數 → 聯盟平均(6分)
    同一位投手在同一次執行中只查一次(用cache),減少API呼叫次數。
    """
    cache = {}
    for m in matchups:
        for side in ("home", "away"):
            score_key = f"{side}_pitcher_score"
            if score_key in m:
                continue
            pid = m.get(f"{side}_pitcher_id")
            name = m.get(f"{side}_pitcher", "")

            score = None
            if pid:
                if pid in cache:
                    score = cache[pid]
                else:
                    try:
                        score = fetch_pitcher_live_score(pid, season)
                    except Exception:
                        score = None
                    cache[pid] = score

            if score is None:
                score = pitcher_ratings.get(name)
            if score is None:
                score = DEFAULT_PITCHER_SCORE
            m[score_key] = score
    return matchups


# ---------------------------------------------------------------------------
# 核心模型
# ---------------------------------------------------------------------------
def season_pct(standings, team):
    w, l = standings.get(team, [0, 0])
    total = w + l
    if total == 0:
        return 0.5
    return w / total


def log5(pa, pb):
    """Bill James Log5 公式"""
    denom = pa + pb - 2 * pa * pb
    if denom == 0:
        return 0.5
    return (pa - pa * pb) / denom


def predict_matchup(standings, pitcher_ratings, injury_scores, matchup):
    home, away = matchup["home"], matchup["away"]

    ph = season_pct(standings, home)
    pa = season_pct(standings, away)

    base = log5(ph, pa)
    odds_base = base / (1 - base) if base < 1 else 999
    odds_home = odds_base * (HOME_FIELD_ADVANTAGE / (1 - HOME_FIELD_ADVANTAGE))
    p_team_strength = odds_home / (1 + odds_home)

    home_pitcher_name = matchup.get("home_pitcher", "TBD")
    away_pitcher_name = matchup.get("away_pitcher", "TBD")
    hp_score = matchup.get("home_pitcher_score", pitcher_ratings.get(home_pitcher_name, DEFAULT_PITCHER_SCORE))
    ap_score = matchup.get("away_pitcher_score", pitcher_ratings.get(away_pitcher_name, DEFAULT_PITCHER_SCORE))
    pitcher_adj = (hp_score - ap_score) * PITCHER_WEIGHT

    home_injury = injury_scores.get(home, DEFAULT_INJURY_SCORE)
    away_injury = injury_scores.get(away, DEFAULT_INJURY_SCORE)
    injury_adj = (away_injury - home_injury) * INJURY_WEIGHT

    p_final = p_team_strength + pitcher_adj + injury_adj
    p_final = max(0.05, min(0.95, p_final))

    return {
        "home": home, "away": away,
        "home_pitcher": home_pitcher_name, "away_pitcher": away_pitcher_name,
        "home_pitcher_score": hp_score, "away_pitcher_score": ap_score,
        "season_pct_home": ph, "season_pct_away": pa,
        "p_team_strength": p_team_strength,
        "pitcher_adj": pitcher_adj, "injury_adj": injury_adj,
        "p_final": p_final,
        "home_injury": home_injury, "away_injury": away_injury,
    }


def run_predictions(standings, pitcher_ratings, injury_scores, matchups):
    results = [predict_matchup(standings, pitcher_ratings, injury_scores, m) for m in matchups]
    results.sort(key=lambda r: -abs(r["p_final"] - 0.5))
    return results


# ---------------------------------------------------------------------------
# 輸出:終端機文字報告
# ---------------------------------------------------------------------------
def print_report(results):
    print("\n" + "=" * 100)
    print("MLB 明日賽事勝率預測報告")
    print("=" * 100)
    print(f"{'主場':6}{'客場':6}{'純戰績':>8}{'完整模型':>10}   主投手(戰力/傷病) vs 客投手(戰力/傷病)")
    print("-" * 100)
    for r in results:
        print(
            f"{r['home']:6}{r['away']:6}"
            f"{r['p_team_strength']*100:7.1f}%"
            f"{r['p_final']*100:9.1f}%   "
            f"{r['home_pitcher']}({r.get('home_pitcher_score', 0):.1f}分/傷{r['home_injury']})  vs  "
            f"{r['away_pitcher']}({r.get('away_pitcher_score', 0):.1f}分/傷{r['away_injury']})"
        )
    print("=" * 100)
    print("備註: 「純戰績」= 僅用 Log5+主場優勢；「完整模型」再疊加先發投手戰力與傷兵嚴重度調整。")
    print("=" * 100 + "\n")


def export_json(results, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"預測結果已匯出至: {path}")


def export_csv(results, path):
    import csv
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "主場", "客場", "主投手", "客投手", "主隊賽季勝率", "客隊賽季勝率",
            "純戰績勝率", "投手調整", "傷兵調整", "完整模型勝率", "主隊傷兵分", "客隊傷兵分",
        ])
        for r in results:
            writer.writerow([
                r["home"], r["away"], r["home_pitcher"], r["away_pitcher"],
                f"{r['season_pct_home']*100:.1f}%", f"{r['season_pct_away']*100:.1f}%",
                f"{r['p_team_strength']*100:.1f}%",
                f"{r['pitcher_adj']*100:+.1f}%", f"{r['injury_adj']*100:+.1f}%",
                f"{r['p_final']*100:.1f}%", r["home_injury"], r["away_injury"],
            ])
    print(f"預測結果已匯出至: {path}")


def save_predictions_history(results, target_date):
    """每次跑預測都自動存一份,供之後 --verify 核對戰果使用"""
    os.makedirs(HISTORY_DIR, exist_ok=True)
    path = os.path.join(HISTORY_DIR, f"{target_date}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    return path


def load_predictions_history(target_date):
    path = os.path.join(HISTORY_DIR, f"{target_date}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 核對戰果:抓實際比分,跟存檔的預測比對
# ---------------------------------------------------------------------------
def fetch_actual_results(id_to_abbr, target_date):
    """
    抓取指定日期所有比賽的實際比分與狀態。
    回傳: [{"home":..,"away":..,"home_score":..,"away_score":..,
            "final": bool, "status": "Final"/"Postponed"/...}, ...]
    """
    url = (
        "https://statsapi.mlb.com/api/v1/schedule"
        f"?sportId=1&date={target_date}&hydrate=linescore"
    )
    print(f"正在抓取 {target_date} 實際賽果...  ({url})")
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read().decode())

    games = []
    for d in data.get("dates", []):
        for game in d.get("games", []):
            teams = game.get("teams", {})
            home = teams.get("home", {})
            away = teams.get("away", {})
            home_id = home.get("team", {}).get("id")
            away_id = away.get("team", {}).get("id")
            home_abbr = _normalize_abbr(id_to_abbr.get(home_id, ""))
            away_abbr = _normalize_abbr(id_to_abbr.get(away_id, ""))
            if not home_abbr or not away_abbr:
                continue
            status = game.get("status", {})
            detailed = status.get("detailedState", "Unknown")
            is_final = status.get("abstractGameState") == "Final"
            games.append({
                "home": home_abbr, "away": away_abbr,
                "home_score": home.get("score"), "away_score": away.get("score"),
                "final": is_final, "status": detailed,
            })
    return games


def compare_predictions(predicted, actual):
    """
    predicted: save_predictions_history 存的那份 list(每筆含 home/away/p_final)
    actual: fetch_actual_results 回傳的 list
    回傳: (比對明細 list, 統計 dict)
    """
    actual_pool = list(actual)  # 複製一份,方便處理雙重賽時逐一消耗
    comparisons = []

    for pred in predicted:
        match_idx = None
        for i, a in enumerate(actual_pool):
            if a["home"] == pred["home"] and a["away"] == pred["away"]:
                match_idx = i
                break
        if match_idx is None:
            comparisons.append({**pred, "matched": False, "status": "查無此場次"})
            continue

        a = actual_pool.pop(match_idx)
        predicted_home_wins = pred["p_final"] >= 0.5

        row = {
            **pred,
            "matched": True,
            "status": a["status"],
            "home_score": a["home_score"],
            "away_score": a["away_score"],
        }

        if a["final"] and a["home_score"] is not None and a["away_score"] is not None:
            actual_home_wins = a["home_score"] > a["away_score"]
            row["final"] = True
            row["actual_home_wins"] = actual_home_wins
            row["correct"] = (predicted_home_wins == actual_home_wins)
        else:
            row["final"] = False
            row["correct"] = None

        comparisons.append(row)

    finals = [c for c in comparisons if c.get("final")]
    correct = [c for c in finals if c.get("correct")]
    stats = {
        "total_games": len(predicted),
        "finished_games": len(finals),
        "correct_count": len(correct),
        "accuracy": (len(correct) / len(finals) * 100) if finals else None,
    }

    # 依信心程度分層看準確度(高信心場次應該要更準才合理)
    high_conf = [c for c in finals if abs(c["p_final"] - 0.5) >= 0.15]
    high_conf_correct = [c for c in high_conf if c.get("correct")]
    stats["high_conf_total"] = len(high_conf)
    stats["high_conf_correct"] = len(high_conf_correct)
    stats["high_conf_accuracy"] = (
        (len(high_conf_correct) / len(high_conf) * 100) if high_conf else None
    )

    return comparisons, stats


def print_verify_report(comparisons, stats, target_date):
    print("\n" + "=" * 100)
    print(f"MLB {target_date} 預測 vs 實際戰果 核對報告")
    print("=" * 100)
    print(f"{'主場':6}{'客場':6}{'預測主隊勝率':>12}{'實際比分':>12}{'結果':>10}")
    print("-" * 100)
    for c in comparisons:
        if not c.get("matched"):
            print(f"{c['home']:6}{c['away']:6}{'--':>12}{'--':>12}{'查無資料':>10}")
            continue
        pred_pct = f"{c['p_final']*100:.1f}%"
        if c.get("final"):
            score = f"{c['home_score']}-{c['away_score']}"
            mark = "✔ 猜對" if c.get("correct") else "✘ 猜錯"
        else:
            score = c.get("status", "未完成")
            mark = "(尚未結束)"
        print(f"{c['home']:6}{c['away']:6}{pred_pct:>12}{score:>12}{mark:>10}")
    print("-" * 100)
    if stats["finished_games"] > 0:
        print(f"整體命中率: {stats['correct_count']}/{stats['finished_games']} "
              f"= {stats['accuracy']:.1f}%")
        if stats["high_conf_total"] > 0:
            print(f"高信心場次(預測勝率≥65%或≤35%)命中率: "
                  f"{stats['high_conf_correct']}/{stats['high_conf_total']} "
                  f"= {stats['high_conf_accuracy']:.1f}%")
    else:
        print("目前沒有已結束的比賽可供核對(可能比賽還在進行中,晚點再跑一次)。")
    print("=" * 100 + "\n")


def generate_verify_html(comparisons, stats, target_date, path):
    rows_html = []
    for c in comparisons:
        if not c.get("matched"):
            rows_html.append(f"""
        <tr><td>{zh(c['home'])}</td><td>{zh(c['away'])}</td>
            <td>--</td><td>--</td><td class="unknown">查無資料</td></tr>""")
            continue
        pred_pct = f"{c['p_final']*100:.1f}%"
        if c.get("final"):
            score = f"{c['home_score']} - {c['away_score']}"
            mark_class = "correct" if c.get("correct") else "wrong"
            mark_text = "✔ 猜對" if c.get("correct") else "✘ 猜錯"
        else:
            score = c.get("status", "未完成")
            mark_class = "unknown"
            mark_text = "尚未結束"
        rows_html.append(f"""
        <tr><td>{zh(c['home'])} <span class="abbr">{c['home']}</span></td>
            <td>{zh(c['away'])} <span class="abbr">{c['away']}</span></td>
            <td>{pred_pct}</td><td>{score}</td>
            <td class="{mark_class}">{mark_text}</td></tr>""")

    acc_line = ""
    if stats["finished_games"] > 0:
        acc_line = (f"整體命中率:{stats['correct_count']}/{stats['finished_games']} "
                    f"= {stats['accuracy']:.1f}%")
        if stats["high_conf_total"] > 0:
            acc_line += (f"　｜　高信心場次命中率:"
                         f"{stats['high_conf_correct']}/{stats['high_conf_total']} "
                         f"= {stats['high_conf_accuracy']:.1f}%")
    else:
        acc_line = "目前沒有已結束的比賽可供核對"

    html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>MLB 預測核對報告 - {target_date}</title>
<style>
  :root {{ --bg:#0f1115; --card:#171a21; --line:#2a2e38; --text:#e8e9ec; --sub:#9aa1ac;
           --good:#4caf50; --bad:#ff6b6b; --neutral:#9aa1ac; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text);
          font-family:-apple-system,"Microsoft JhengHei","PingFang TC",sans-serif;
          padding:24px 16px 60px; }}
  h1 {{ text-align:center; font-size:22px; margin-bottom:4px; }}
  .date {{ text-align:center; color:var(--sub); margin-bottom:12px; font-size:14px; }}
  .stats {{ text-align:center; color:var(--text); font-size:15px; margin-bottom:24px;
            background:var(--card); max-width:900px; margin-left:auto; margin-right:auto;
            padding:12px; border-radius:10px; }}
  table {{ width:100%; max-width:900px; margin:0 auto; border-collapse:collapse;
           background:var(--card); border-radius:12px; overflow:hidden; }}
  td {{ padding:12px 10px; border-bottom:1px solid var(--line); font-size:14px; text-align:center; }}
  tr:last-child td {{ border-bottom:none; }}
  .abbr {{ color:var(--sub); font-size:11px; }}
  .correct {{ color:var(--good); font-weight:700; }}
  .wrong {{ color:var(--bad); font-weight:700; }}
  .unknown {{ color:var(--neutral); }}
  @media (max-width:700px) {{
    table, tbody, tr, td {{ display:block; width:100%; }}
    tr {{ margin-bottom:10px; border:1px solid var(--line); border-radius:10px; padding:6px; }}
    td {{ border:none; text-align:left; padding:4px 8px; }}
  }}
</style>
</head>
<body>
  <h1>📊 MLB 預測核對報告</h1>
  <div class="date">{target_date} 賽事</div>
  <div class="stats">{acc_line}</div>
  <table><tbody>{''.join(rows_html)}</tbody></table>
  <div style="text-align:center;margin-top:20px;">
    <a href="index.html" style="color:#4f8cff">回首頁</a>　
    <a href="report.html" style="color:#4f8cff">今日預測</a>　
    <a href="stats.html" style="color:#4f8cff">累積準確率</a>
  </div>
</body>
</html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


# ---------------------------------------------------------------------------
# 輸出:網頁報告(方便每天用瀏覽器直接看)
# ---------------------------------------------------------------------------
def generate_html(results, target_date, path):
    rows_html = []
    for r in results:
        favored_home = r["p_final"] >= 0.5
        home_pct = r["p_final"] * 100
        away_pct = (1 - r["p_final"]) * 100
        hp_score = r.get("home_pitcher_score")
        ap_score = r.get("away_pitcher_score")
        hp_score_str = f"戰力{hp_score:.1f} / " if hp_score is not None else ""
        ap_score_str = f"戰力{ap_score:.1f} / " if ap_score is not None else ""
        rows_html.append(f"""
        <tr>
          <td class="team {'favored' if favored_home else ''}">{zh(r['home'])} <span class="abbr">{r['home']}</span></td>
          <td class="pitcher">{r['home_pitcher']}<br><span class="sub">{hp_score_str}傷病分{r['home_injury']}</span></td>
          <td class="prob {'favored' if favored_home else ''}">{home_pct:.1f}%</td>
          <td class="vs">vs</td>
          <td class="prob {'favored' if not favored_home else ''}">{away_pct:.1f}%</td>
          <td class="pitcher">{r['away_pitcher']}<br><span class="sub">{ap_score_str}傷病分{r['away_injury']}</span></td>
          <td class="team {'favored' if not favored_home else ''}">{zh(r['away'])} <span class="abbr">{r['away']}</span></td>
          <td class="bar-cell">
            <div class="bar-track">
              <div class="bar-fill" style="width:{home_pct:.1f}%"></div>
            </div>
          </td>
        </tr>""")

    html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>MLB 勝率預測報告 - {target_date}</title>
<style>
  :root {{
    --bg: #0f1115; --card: #171a21; --line: #2a2e38;
    --text: #e8e9ec; --sub: #9aa1ac; --accent: #4f8cff; --accent2: #ff6b6b;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin:0; background: var(--bg); color: var(--text);
    font-family: -apple-system, "Microsoft JhengHei", "PingFang TC", sans-serif;
    padding: 24px 16px 60px;
  }}
  h1 {{ text-align:center; font-size: 22px; margin-bottom:4px; }}
  .date {{ text-align:center; color: var(--sub); margin-bottom: 24px; font-size:14px; }}
  table {{
    width: 100%; max-width: 900px; margin: 0 auto; border-collapse: collapse;
    background: var(--card); border-radius: 12px; overflow: hidden;
  }}
  td {{ padding: 12px 10px; border-bottom: 1px solid var(--line); font-size: 14px; text-align:center; }}
  tr:last-child td {{ border-bottom: none; }}
  .team {{ font-weight: 600; min-width: 90px; }}
  .team.favored {{ color: var(--accent); }}
  .abbr {{ color: var(--sub); font-weight: 400; font-size: 11px; }}
  .pitcher {{ color: var(--sub); font-size: 12px; min-width:120px; }}
  .pitcher .sub {{ font-size: 10px; opacity:0.7; }}
  .prob {{ font-size: 18px; font-weight: 700; min-width: 70px; color: var(--sub); }}
  .prob.favored {{ color: var(--accent); }}
  .vs {{ color: var(--sub); font-size: 12px; }}
  .bar-cell {{ min-width: 100px; }}
  .bar-track {{ height: 8px; border-radius: 4px; background: var(--accent2); overflow:hidden; }}
  .bar-fill {{ height: 100%; background: var(--accent); }}
  .footer {{ max-width:900px; margin: 20px auto 0; color: var(--sub); font-size: 12px; text-align:center; line-height:1.6; }}
  @media (max-width: 700px) {{
    table, tbody, tr, td {{ display:block; width:100%; }}
    tr {{ margin-bottom: 14px; border: 1px solid var(--line); border-radius: 10px; padding: 8px; }}
    td {{ border:none; text-align:left; padding: 4px 8px; }}
    .bar-cell {{ padding-top:8px; }}
  }}
</style>
</head>
<body>
  <h1>⚾ MLB 勝率預測報告</h1>
  <div class="date">{target_date} 賽事｜依信心程度排序｜藍色=較被看好</div>
  <table>
    <tbody>
      {''.join(rows_html)}
    </tbody>
  </table>
  <div class="footer">
    模型:Log5(賽季勝率)+ 主場優勢 + 先發投手戰力調整 + 傷兵嚴重度調整。<br>
    此為簡化量化模型,僅供參考,不構成投注建議。<br>
    <a href="index.html" style="color:var(--accent2)">回首頁</a>　
    <a href="verify_report.html" style="color:var(--accent2)">核對報告</a>　
    <a href="stats.html" style="color:var(--accent2)">累積準確率</a>
  </div>
</body>
</html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


# ---------------------------------------------------------------------------
# 累積準確率統計(掃描所有已存檔的預測記錄,逐日抓實際戰果並彙總)
# ---------------------------------------------------------------------------
def compute_cumulative_stats():
    """
    掃描 data/history 裡所有存過的預測記錄,對每個日期呼叫 MLB API 抓實際戰果,
    逐日比對後彙總成累積至今的整體命中率。
    """
    if not os.path.isdir(HISTORY_DIR):
        return [], {"total_days": 0, "finished_games": 0, "correct_count": 0, "accuracy": None}

    history_dates = sorted(f[:-5] for f in os.listdir(HISTORY_DIR) if f.endswith(".json"))
    if not history_dates:
        return [], {"total_days": 0, "finished_games": 0, "correct_count": 0, "accuracy": None}

    try:
        id_to_abbr = fetch_team_abbr_map()
    except Exception as e:
        print(f"[錯誤] 無法連線抓取戰果 ({e})。請確認網路連線正常。")
        return [], {"total_days": 0, "finished_games": 0, "correct_count": 0, "accuracy": None}

    per_day = []
    all_finals = []
    for d in history_dates:
        predicted = load_predictions_history(d)
        if not predicted:
            continue
        try:
            actual = fetch_actual_results(id_to_abbr, d)
        except Exception as e:
            print(f"[警告] {d} 抓取戰果失敗 ({e}),跳過這天")
            continue
        comparisons, stats = compare_predictions(predicted, actual)
        per_day.append((d, stats))
        all_finals.extend([c for c in comparisons if c.get("final")])

    correct = [c for c in all_finals if c.get("correct")]
    high_conf = [c for c in all_finals if abs(c["p_final"] - 0.5) >= 0.15]
    high_conf_correct = [c for c in high_conf if c.get("correct")]

    overall = {
        "total_days": len(per_day),
        "finished_games": len(all_finals),
        "correct_count": len(correct),
        "accuracy": (len(correct) / len(all_finals) * 100) if all_finals else None,
        "high_conf_total": len(high_conf),
        "high_conf_correct": len(high_conf_correct),
        "high_conf_accuracy": (len(high_conf_correct) / len(high_conf) * 100) if high_conf else None,
    }
    return per_day, overall


def print_cumulative_stats(per_day, overall):
    print("\n" + "=" * 70)
    print("MLB 累積命中率統計(至今所有已存檔預測日期)")
    print("=" * 70)
    if overall["total_days"] == 0:
        print("目前還沒有任何可核對的預測記錄。")
        print("請先用本程式跑過幾天預測(會自動存進 data/history/),")
        print("累積幾天資料、等比賽打完後再回來看這份統計。")
        print("=" * 70 + "\n")
        return

    print(f"{'日期':12}{'場次':>6}{'猜對':>6}{'命中率':>10}")
    print("-" * 70)
    for d, stats in per_day:
        if stats["finished_games"] > 0:
            print(f"{d:12}{stats['finished_games']:>6}{stats['correct_count']:>6}"
                  f"{stats['accuracy']:>9.1f}%")
        else:
            print(f"{d:12}{'--':>6}{'--':>6}{'無資料':>10}")
    print("-" * 70)
    print(f"已核對天數: {overall['total_days']} 天")
    if overall["finished_games"]:
        print(f"累積整體命中率: {overall['correct_count']}/{overall['finished_games']} "
              f"= {overall['accuracy']:.1f}%")
    else:
        print("累積整體命中率: 無資料")
    if overall["high_conf_total"] > 0:
        print(f"累積高信心場次命中率: {overall['high_conf_correct']}/{overall['high_conf_total']} "
              f"= {overall['high_conf_accuracy']:.1f}%")
    print("=" * 70 + "\n")


def generate_stats_html(per_day, overall, path):
    if overall["total_days"] == 0:
        body = '<p class="empty">目前還沒有任何可核對的預測記錄,累積幾天資料後再回來看。</p>'
        summary_line = ""
    else:
        rows = []
        for d, stats in per_day:
            if stats["finished_games"] > 0:
                rows.append(f"""<tr><td>{d}</td><td>{stats['finished_games']}</td>
                    <td>{stats['correct_count']}</td><td>{stats['accuracy']:.1f}%</td></tr>""")
            else:
                rows.append(f"<tr><td>{d}</td><td>--</td><td>--</td><td class='unknown'>無資料</td></tr>")
        body = f"""
        <table><thead><tr><th>日期</th><th>場次</th><th>猜對</th><th>命中率</th></tr></thead>
        <tbody>{''.join(rows)}</tbody></table>"""
        summary_line = f"已核對天數:{overall['total_days']} 天　｜　"
        if overall["finished_games"]:
            summary_line += f"累積整體命中率:{overall['correct_count']}/{overall['finished_games']} = {overall['accuracy']:.1f}%"
        if overall["high_conf_total"] > 0:
            summary_line += (f"　｜　高信心場次命中率:{overall['high_conf_correct']}/{overall['high_conf_total']} "
                              f"= {overall['high_conf_accuracy']:.1f}%")

    html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>MLB 累積準確率統計</title>
<style>
  :root {{ --bg:#0f1115; --card:#171a21; --line:#2a2e38; --text:#e8e9ec; --sub:#9aa1ac; --accent:#4f8cff; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text);
          font-family:-apple-system,"Microsoft JhengHei","PingFang TC",sans-serif;
          padding:24px 16px 60px; }}
  h1 {{ text-align:center; font-size:22px; margin-bottom:16px; }}
  .summary {{ text-align:center; color:var(--accent); font-size:15px; margin-bottom:24px;
              background:var(--card); max-width:800px; margin-left:auto; margin-right:auto;
              padding:14px; border-radius:10px; font-weight:600; }}
  .empty {{ text-align:center; color:var(--sub); }}
  table {{ width:100%; max-width:800px; margin:0 auto; border-collapse:collapse;
           background:var(--card); border-radius:12px; overflow:hidden; }}
  th, td {{ padding:10px; border-bottom:1px solid var(--line); font-size:14px; text-align:center; }}
  th {{ color:var(--sub); font-weight:600; }}
  tr:last-child td {{ border-bottom:none; }}
  .unknown {{ color:var(--sub); }}
  .back {{ display:block; text-align:center; margin-top:24px; }}
  .back a {{ color:var(--accent); text-decoration:none; }}
</style>
</head>
<body>
  <h1>📈 MLB 累積準確率統計</h1>
  {f'<div class="summary">{summary_line}</div>' if summary_line else ''}
  {body}
  <div class="back"><a href="index.html">← 回首頁</a></div>
</body>
</html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


# ---------------------------------------------------------------------------
# 主程式進入點
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="MLB 每日勝率預測系統")
    parser.add_argument("--live", action="store_true", help="自動抓取即時戰績與對戰表(需網路)")
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="指定要預測的日期(預設: 明天)")
    parser.add_argument("--html", nargs="?", const=DEFAULT_HTML_OUTPUT, metavar="FILE",
                         help="輸出網頁報告並自動用瀏覽器開啟(可指定路徑,預設 report.html)")
    parser.add_argument("--no-open", action="store_true", help="搭配 --html 使用時,只產生檔案不自動開啟瀏覽器")
    parser.add_argument("--export-json", metavar="FILE", help="將結果輸出為 JSON 檔")
    parser.add_argument("--export-csv", metavar="FILE", help="將結果輸出為 CSV 檔(可用 Excel 開啟)")
    parser.add_argument("--verify", nargs="?", const="__YESTERDAY__", metavar="YYYY-MM-DD",
                         help="核對某天的預測是否命中實際戰果(不指定日期則預設查昨天)")
    parser.add_argument("--stats", action="store_true",
                         help="顯示至今所有已存檔預測日期的累積命中率統計")
    args = parser.parse_args()

    # -----------------------------------------------------------------
    # 累積準確率統計模式
    # -----------------------------------------------------------------
    if args.stats:
        per_day, overall = compute_cumulative_stats()
        print_cumulative_stats(per_day, overall)
        if args.html:
            html_path = args.html if args.html != DEFAULT_HTML_OUTPUT else os.path.join(BASE_DIR, "stats.html")
            generate_stats_html(per_day, overall, html_path)
            print(f"統計網頁已產生: {html_path}")
            if not args.no_open:
                webbrowser.open(f"file://{os.path.abspath(html_path)}")
        return

    # -----------------------------------------------------------------
    # 核對戰果模式
    # -----------------------------------------------------------------
    if args.verify is not None:
        verify_date = args.verify
        if verify_date == "__YESTERDAY__":
            verify_date = (date.today() - timedelta(days=1)).isoformat()

        predicted = load_predictions_history(verify_date)
        if predicted is None:
            print(f"[錯誤] 找不到 {verify_date} 的預測記錄(data/history/{verify_date}.json 不存在)。")
            print("提醒: 只有用本程式跑過預測的日期才會有記錄可供核對。")
            sys.exit(1)

        try:
            id_to_abbr = fetch_team_abbr_map()
            actual = fetch_actual_results(id_to_abbr, verify_date)
        except Exception as e:
            print(f"[錯誤] 無法連線抓取 {verify_date} 的實際賽果 ({e})。")
            print("請確認網路連線正常,稍後再試一次 --verify。")
            sys.exit(1)
        comparisons, stats = compare_predictions(predicted, actual)
        print_verify_report(comparisons, stats, verify_date)

        if args.html:
            html_path = args.html if args.html != DEFAULT_HTML_OUTPUT else DEFAULT_VERIFY_HTML_OUTPUT
            generate_verify_html(comparisons, stats, verify_date, html_path)
            print(f"核對報告網頁已產生: {html_path}")
            if not args.no_open:
                webbrowser.open(f"file://{os.path.abspath(html_path)}")
        return

    # -----------------------------------------------------------------
    # 一般預測模式
    # -----------------------------------------------------------------
    target_date = args.date or (date.today() + timedelta(days=1)).isoformat()

    if args.live:
        try:
            id_to_abbr = fetch_team_abbr_map()
        except Exception as e:
            print(f"[警告] 無法取得球隊對照表 ({e}),即時抓取將全部改用本地資料")
            id_to_abbr = {}
        standings = fetch_live_standings(id_to_abbr)
        matchups = fetch_live_matchups(id_to_abbr, target_date)
    else:
        standings = load_json(STANDINGS_FILE)
        matchups = load_json(MATCHUPS_FILE)

    pitcher_ratings = load_json(PITCHER_FILE)
    injury_scores = load_json(INJURY_FILE)

    if args.live:
        season = int(target_date[:4])
        print(f"正在計算 {season} 賽季先發投手真實戰力(依FIP換算)...")
        try:
            matchups = fetch_pitcher_scores_for_matchups(matchups, season, pitcher_ratings)
        except Exception as e:
            print(f"[警告] 投手戰力計算過程發生錯誤 ({e}),部分投手可能改用備援分數")

    results = run_predictions(standings, pitcher_ratings, injury_scores, matchups)
    print_report(results)
    history_path = save_predictions_history(results, target_date)
    print(f"預測記錄已存檔(供之後 --verify 核對用): {history_path}")

    if args.export_json:
        export_json(results, args.export_json)
    if args.export_csv:
        export_csv(results, args.export_csv)
    if args.html:
        html_path = generate_html(results, target_date, args.html)
        print(f"網頁報告已產生: {html_path}")
        if not args.no_open:
            webbrowser.open(f"file://{os.path.abspath(html_path)}")


if __name__ == "__main__":
    main()
