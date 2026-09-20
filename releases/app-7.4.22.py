import hashlib
import io
import json
import html
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import queue
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests

try:
    from plyer import notification
except Exception:
    notification = None

APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "config.json"
DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
APP_VERSION = "7.4.22"
UPDATER_CONFIG_FILE = APP_DIR / "updater_config.json"
DEFAULT_UPDATE_MANIFEST_URL = (
    "https://raw.githubusercontent.com/scratch-an/"
    "youtube-live-monitor-updates/main/update_manifest.json"
)


def ensure_update_only_bat():
    """同じフォルダに、更新確認だけを行うWindows用BATを用意する。"""
    if os.name != "nt":
        return
    bat_path = APP_DIR / "最新版へ更新.bat"
    content = """@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "APP_PY=%~dp0app.py"
if exist "%~dp0.venv\\Scripts\\python.exe" (
  "%~dp0.venv\\Scripts\\python.exe" "%APP_PY%" --update-only
) else (
  py "%APP_PY%" --update-only
)
echo.
pause
"""
    try:
        if not bat_path.exists() or bat_path.read_text(encoding="utf-8-sig") != content:
            bat_path.write_text(content, encoding="utf-8-sig")
    except OSError as e:
        print(f"⚠️ 更新専用BATを作成できません: {e}")


def version_tuple(value):
    numbers = re.findall(r"\d+", str(value))
    return tuple(int(x) for x in numbers[:4]) or (0,)


def load_updater_config():
    """自動更新設定を読み込む。旧OneDrive設定も引き続き利用できる。"""
    updater_cfg = {}
    if UPDATER_CONFIG_FILE.exists():
        try:
            updater_cfg = json.loads(UPDATER_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠️ updater_config.jsonを読めません: {e}")
    return updater_cfg


def resolve_update_folder(updater_cfg=None):
    """設定値、またはWindowsのOneDrive同期先から更新フォルダを探す。"""
    updater_cfg = updater_cfg or {}
    configured = ""
    configured = str(updater_cfg.get("source_folder", "")).strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()

    for env_name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        root = os.environ.get(env_name, "").strip()
        if root:
            return Path(root) / "YouTube会見監視_更新"
    return None


def check_for_updates():
    """GitHubまたはOneDriveから、安全確認後にapp.pyを自己更新する。"""
    skip_flag = "--skip-update-check"
    if skip_flag in sys.argv:
        sys.argv.remove(skip_flag)
        return

    updater_cfg = load_updater_config()
    if not bool(updater_cfg.get("enabled", True)):
        return

    manifest_url = str(
        updater_cfg.get("manifest_url", DEFAULT_UPDATE_MANIFEST_URL)
    ).strip()
    source_dir = resolve_update_folder(updater_cfg)

    temp_path = APP_DIR / ".app_update.tmp.py"
    backup_path = APP_DIR / "app.py.backup"
    target_path = Path(__file__).resolve()
    replaced = False
    try:
        if manifest_url:
            response = requests.get(
                manifest_url,
                timeout=(5, 20),
                headers={"Cache-Control": "no-cache"},
            )
            response.raise_for_status()
            manifest = response.json()
        else:
            if source_dir is None:
                print("ℹ️ 自動更新先を検出できません。自動更新を省略します。")
                return
            manifest_path = source_dir / "update_manifest.json"
            if not manifest_path.exists():
                print(f"ℹ️ 自動更新ファイルはまだありません: {source_dir}")
                return
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        latest_version = str(manifest.get("version", "0"))
        if version_tuple(latest_version) <= version_tuple(APP_VERSION):
            print(f"✅ 最新版です（バージョン {APP_VERSION}）")
            return

        file_name = Path(str(manifest.get("file", "app.py"))).name
        expected_hash = str(manifest.get("sha256", "")).strip().lower()
        if not expected_hash:
            raise RuntimeError("更新情報にSHA-256がありません")

        if manifest_url:
            file_url = str(manifest.get("url", "")).strip()
            if not file_url:
                file_url = manifest_url.rsplit("/", 1)[0] + "/" + file_name
            file_response = requests.get(file_url, timeout=(5, 60))
            file_response.raise_for_status()
            source_bytes = file_response.content
        else:
            source_file = source_dir / file_name
            if not source_file.is_file():
                raise RuntimeError("更新ファイルがありません")
            source_bytes = source_file.read_bytes()

        actual_hash = hashlib.sha256(source_bytes).hexdigest()
        if actual_hash != expected_hash:
            raise RuntimeError("更新ファイルのSHA-256が一致しません")

        source_text = source_bytes.decode("utf-8")
        version_match = re.search(r'^APP_VERSION\s*=\s*[\"\']([^\"\']+)', source_text, re.M)
        if not version_match or version_match.group(1) != latest_version:
            raise RuntimeError("更新情報とapp.pyのバージョンが一致しません")

        temp_path.write_bytes(source_bytes)
        subprocess.run(
            [sys.executable, "-m", "py_compile", str(temp_path)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True
        )
        shutil.copy2(target_path, backup_path)
        os.replace(temp_path, target_path)
        replaced = True
        print(f"⬆️ バージョン {APP_VERSION} → {latest_version} に更新しました。再起動します。")
        os.execv(
            sys.executable,
            [sys.executable, str(target_path)] + sys.argv[1:] + [skip_flag]
        )
    except Exception as e:
        print(f"⚠️ 自動更新を適用できませんでした。現在版で起動します: {e}")
        if replaced and backup_path.exists():
            try:
                shutil.copy2(backup_path, target_path)
                print("   更新前のapp.pyへ戻しました。")
            except Exception as restore_error:
                print(f"   バックアップ復元にも失敗しました: {restore_error}")
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass

DEFAULT_CONFIG = {
    "youtube_api_key": "",
    # 一時停止中。trueに戻すとベッセント長官のLive監視を再開する。
    "enable_bessent_monitoring": True,
    # search.list は1日100回までのため、20分未満にはしない。
    # 20分間隔なら最大72回/日となり、手動テスト分も残せる。
    "scan_interval_minutes": 20,
    # 予約Liveは開始前に拾えるため、1時間間隔で十分。
    # live検索72回 + upcoming検索24回 = 最大96回/日（9,600クォータ単位）。
    "upcoming_scan_interval_minutes": 60,
    # G20・G7・IMF等のイベント日は、海外時間に備えて24時間15分間隔で
    # 配信中Liveを検索する。その日は予約検索を省略し、96回/日に収める。
    "event_live_scan_interval_minutes": 15,
    "event_schedule_check_hours": 6,
    "event_schedule_urls": [
        "https://www.mof.go.jp/public_relations/weekly_schedule/index.htm",
        "https://g20.org/events-calendar/",
        "https://www.boj.or.jp/",
        "https://home.treasury.gov/news/press-releases/statements-remarks/secretary"
    ],
    "event_keywords": [
        "G20", "G7", "IMF", "国際通貨基金", "年次総会",
        "財務大臣・中央銀行総裁", "Finance Ministers",
        "Central Bank Governors", "Annual Meetings",
        "総裁記者会見ライブ配信", "金融政策決定会合",
        "審議委員", "副総裁", "Bessent", "ベッセント",
        "House Financial Services Committee"
    ],
    # 公式ページで拾えない予定は "2026-10-15" の形式で追加できる。
    "manual_event_dates": [],
    "search_query": (
        "高市総理|片山財務大臣|三村財務官|"
        "植田総裁|内田副総裁|氷見野副総裁|高田審議委員|"
        "田村審議委員|小枝審議委員|増審議委員|"
        "浅田審議委員|佐藤審議委員|Scott Bessent|Bessent"
    ),
    "search_terms": [
        "高市", "高市総理", "高市内閣", "内閣総理大臣",
        "片山", "片山財務大臣", "財務大臣",
        "三村", "三村財務官", "財務官",
        "植田和男", "植田総裁", "日銀総裁",
        "内田眞一", "内田副総裁", "氷見野良三", "氷見野副総裁",
        "高田創", "高田審議委員", "田村直樹", "田村審議委員",
        "小枝淳子", "小枝審議委員", "増一行", "増審議委員",
        "浅田統一郎", "浅田審議委員", "佐藤綾野", "佐藤審議委員",
        "Scott Bessent", "Secretary Bessent", "Bessent", "ベッセント"
    ],
    # Live形式でも、録画の見逃し配信として公開されたものは除外する。
    "excluded_title_terms": ["見逃し配信", "見逃しライブ"],
    # 個人チャンネルを除外し、官公庁・中央銀行・報道機関だけを許可する。
    "organization_channel_terms": [
        "首相官邸", "政府広報", "内閣府", "財務省", "Ministry of Finance",
        "日本銀行", "Bank of Japan", "国会", "衆議院", "参議院",
        "NHK", "日テレNEWS", "ANNnewsCH", "テレビ朝日", "TBS NEWS",
        "JNN", "FNN", "フジテレビ", "テレ東BIZ", "テレビ東京",
        "共同通信", "KYODO", "時事通信", "THE PAGE", "ニコニコニュース",
        "Bloomberg", "Reuters", "Associated Press", "AP Archive",
        "Forbes", "C-SPAN", "CNBC", "CNN", "BBC", "Financial Times"
    ],
    "region_code": "JP",
    "language": "ja",
    "transcription_model": "small",
    "english_transcription_model": "medium",
    "download_from_start": True,
    "cookies_from_browser": "",
    "keep_audio": True,
    "chunk_seconds": 20,
    "beam_size": 8,
    "sentence_merge_max_seconds": 30,
    "speaker_labeling": True,
    "translate_english_to_japanese": True,
    "save_english_original": True,
    # 英語原文はTXTへ保存するが、画面には日本語訳だけを表示する。
    "display_english_original": False,
    "translation_timeout_seconds": 20,
    "translation_cooldown_minutes": 30,
    # OpenAI翻訳。APIキーは config.json ではなく .env の
    # OPENAI_API_KEY に保存する。
    "openai_translation_enabled": True,
    "openai_model": "gpt-5.6-luna",
    "openai_critical_model": "gpt-5.6-terra",
    "monthly_translation_budget_jpy": 1500,
    # 為替変動と料金誤差を吸収するため、実勢より高めの換算値を使用。
    "budget_usd_to_jpy": 170.0,
    # 2026-09-17時点のStandard料金。料金改定時はここを更新する。
    "openai_input_usd_per_million": 0.10,
    "openai_output_usd_per_million": 0.60,
    "openai_critical_input_usd_per_million": 1.00,
    "openai_critical_output_usd_per_million": 6.00,
    "openai_request_timeout_seconds": 30,
    # 自動投稿は行わず、コピーしやすい投稿補助ウィンドウへ候補をためる。
    "important_statement_marking": True,
    "post_assistant_enabled": True,
    "post_assistant_max_chars": 180,
    # 最後だけ短い分割になる場合は、この長さまで直前投稿との結合を許容する。
    "post_assistant_tail_merge_max_chars": 260,
    "post_assistant_always_on_top": True,
    "ai_post_boundary_enabled": True,
    "ai_post_min_chars": 20,
    # 重要発言も短文だけで即確定せず、理由・方針まで少し待ってまとめる。
    "ai_post_important_min_chars": 80,
    # 通常の短文は同じ話題の後続文を待ってから、ひとまとまりにする。
    # 市場に影響する重要発言は ai_post_min_chars で早めに表示する。
    "ai_post_group_min_chars": 90,
    "ai_post_max_buffer_chars": 500,
    # YouTubeキーワード検索から漏れやすい海外報道チャンネルを、
    # APIクォータを使わない公式フィードで補助監視する。
    "priority_feed_interval_minutes": 2,
    "priority_channel_ids": {
        "Forbes Breaking News": "UCg40OxZ1GYh3u3jBntB6DLg",
        "Associated Press": "UC52X5wxOL_s5yw0dQk7NtgA",
        "Reuters": "UChqUTb7kYRX8-EiaN3XFrSQ",
        "C-SPAN": "UCb--64Gl51jIEVE-GLDAVTg"
    },
    "default_official_speaker": "高市総理",
    "archive_official_speakers": {
        "oEqKmehjjvA": "高市総理",
        "SmIRQKIuCEw": "片山さつき 財務大臣",
        "mzklpHRM4m8": "植田和男 日銀総裁",
        "TuI4euaLfuE": "片山さつき 財務大臣"
    },
    # 公的機関が公開した会見概要。アーカイブ時だけ認識補正に利用する。
    "archive_official_reference_enabled": True,
    "archive_official_reference_urls": {
        "TuI4euaLfuE": "https://www.mof.go.jp/public_relations/conference/my20260915.html"
    },
    # 予約LIVEの開始5分前から、公式資料の先行公開を1分間隔で確認する。
    "official_pre_live_monitor_enabled": True,
    "official_pre_live_monitor_minutes": 5,
    "official_pre_live_monitor_interval_seconds": 60,
    "official_pre_live_monitor_after_start_minutes": 10,
    "official_live_source_urls": {
        "mof": "https://www.mof.go.jp/public_relations/conference/index.html",
        "boj": "https://www.boj.or.jp/"
    },
    "archive_speaker_ranges": {
        "oEqKmehjjvA": [
            {"start": "00:19:41", "end": "00:21:00", "speaker": "記者"},
            {"start": "00:21:01", "end": "00:28:13", "speaker": "高市総理"},
            {"start": "00:28:20", "end": "00:28:36", "speaker": "記者"}
        ],
        "SmIRQKIuCEw": [
            {"start": "00:00:20", "end": "00:00:25", "speaker": "司会"},
            {"start": "00:03:40", "end": "00:04:48", "speaker": "片山さつき 財務大臣"}
        ],
        "TuI4euaLfuE": [
            # 冒頭40秒には大臣→幹事社→大臣の切替があるため、
            # 固定時間範囲ではなく発話内容から判定する。
            {"start": "00:00:40", "end": "00:03:59", "speaker": "片山さつき 財務大臣"},
            {"start": "00:04:00", "end": "00:04:37", "speaker": "記者"},
            {"start": "00:04:38", "end": "00:06:13", "speaker": "片山さつき 財務大臣"},
            {"start": "00:06:14", "end": "00:06:19", "speaker": "司会"},
            {"start": "00:06:20", "end": "00:06:48", "speaker": "NHK 佐藤記者"},
            {"start": "00:06:49", "end": "00:09:09", "speaker": "片山さつき 財務大臣"},
            {"start": "00:09:10", "end": "00:09:32", "speaker": "ブルームバーグ 横山記者"},
            {"start": "00:09:33", "end": "00:10:31", "speaker": "片山さつき 財務大臣"},
            {"start": "00:10:32", "end": "00:11:06", "speaker": "読売新聞 田中記者"},
            {"start": "00:11:07", "end": "00:12:10", "speaker": "片山さつき 財務大臣"},
            {"start": "00:12:11", "end": "00:12:59", "speaker": "朝日新聞 長谷記者"},
            {"start": "00:13:00", "end": "00:13:38", "speaker": "片山さつき 財務大臣"},
            {"start": "00:13:39", "end": "00:14:12", "speaker": "記者"},
            {"start": "00:14:13", "end": "00:14:19", "speaker": "片山さつき 財務大臣"},
            {"start": "00:14:20", "end": "00:14:34", "speaker": "記者"},
            {"start": "00:14:35", "end": "00:14:45", "speaker": "片山さつき 財務大臣"},
            {"start": "00:14:46", "end": "00:14:53", "speaker": "記者"},
            {"start": "00:14:54", "end": "00:14:58", "speaker": "片山さつき 財務大臣"},
            {"start": "00:14:59", "end": "00:15:12", "speaker": "司会"}
        ]
    }
}

# 会見で誤認識されやすい「語彙」だけをWhisperへ軽く教える。
# 完成文を入れると反復幻覚を誘発しやすいため、V4では短い固有名詞・専門語に限定。
WHISPER_PROMPT = (
    "日本政府・日本銀行の記者会見と講演。高市総理、片山さつき財務大臣、三村財務官、財務省、"
    "日本銀行、日銀、植田和男総裁、内田眞一副総裁、氷見野良三副総裁、"
    "高田創審議委員、田村直樹審議委員、小枝淳子審議委員、増一行審議委員、"
    "浅田統一郎審議委員、佐藤綾野審議委員、金融政策決定会合、展望レポート、"
    "閣議後記者会見、幹事社、2027年国際園芸博覧会、"
    "為替市場、為替相場、ファンダメンタルズ、ベッセント財務長官、米国財務省、"
    "日銀、為替介入、日米協調介入、秩序ある為替市場、政策立案者、"
    "ウォールストリート・ジャーナル、ヘッジファンド、原油価格、中東情勢、ホルムズ海峡、"
    "石油備蓄、代替調達、診療報酬改定、介護報酬改定、一般会計、新規国債発行額、公債依存度、"
    "ガソリン税、軽油引取税、物価高騰対策、"
    "政策当局、独立性、ダイモン会長、同博覧会、日本の原産植物。"
)

ENGLISH_WHISPER_PROMPT = (
    "U.S. Treasury Secretary Scott Bessent. House Financial Services Committee. "
    "U.S. economy, international financial system, Treasury yields, fiscal policy, "
    "Federal Reserve, Bank of Japan, Japanese yen, foreign exchange market, IMF, G20."
)

# 一般化しても危険が小さい、確認済みの崩れだけ。
TEXT_CORRECTIONS = [
    ("ぶっか田駿の対応", "物価高騰対策の対応"),
    ("その来一歩", "その第一歩"),
    ("物価高屋", "物価高へ"),
    ("経由引き取り出", "軽油引取税"),
    ("報酬回転", "報酬改定"),
    ("中等情勢", "中東情勢"),
    ("劇変換話措置", "激変緩和措置"),
    ("高位置内閣", "高市内閣"),
    ("原油加工夫", "原油確保"),
    ("臨居を変に", "臨機応変に"),
    ("漢字社", "幹事社"),
    ("政策権利… であります、ウタンポコールレート・オバナイトモノ", "政策金利であります、無担保コールレート（オーバーナイト物）"),
    ("ウタンポコールレート・オバナイトモノ", "無担保コールレート（オーバーナイト物）"),
    ("保管党在寄金制度", "補完当座預金制度"),
    ("適用理理", "適用利率"),
    ("基準貸付履歴", "基準貸付利率"),
]

# 特定アーカイブでユーザーが確認した訂正。
# 他動画へ誤適用しないよう、動画IDごとに分離する。
VIDEO_TEXT_CORRECTIONS = {
    "oEqKmehjjvA": [
        ("ホルムズ会計", "ホルムズ海峡"),
        ("長達", "調達"),
        ("大体調達", "代替調達"),
        ("大体長達", "代替調達"),
        ("メドウ", "目途"),
        ("診療報酬書いて", "診療報酬改定"),
        ("介護報酬書いて", "介護報酬改定"),
        ("国の一般会見", "国の一般会計"),
        ("新規国債発行額、", "新規国債発行額を、"),
        ("後載依存度", "公債依存度"),
        ("経由引き取り税", "軽油引取税"),
        ("使用料の多い", "使用量の多い"),
    ],
    "SmIRQKIuCEw": [
        ("法律大規則会見", "閣議後記者会見"),
        ("国際NJ博覧会", "国際園芸博覧会"),
        ("表面が銅白欄", "同博覧会"),
        ("漢字社", "幹事社"),
        ("カンダメイカル", "ファンダメンタル"),
        ("かわせ調査", "為替相場"),
        ("デセンと大臣長官", "ベッセント財務長官"),
        ("別戦と長官", "ベッセント長官"),
        ("化石市場", "為替市場"),
        ("日米強調会議", "日米協調介入"),
        ("出場ある川瀬市場", "秩序ある為替市場"),
        ("栄物情報", "英語の情報"),
        ("具体的に研究するということ", "具体的に言及するとこと"),
        ("一切変わっております", "一切変わっておりません"),
        ("マーケットをすらない情報", "マーケットしらない情報"),
        ("指さされますが", "示唆されました"),
        ("自分はドーマとダッとも", "その際自分は、胴元だとも"),
        # V4: SmIRQKIuCEw 再テストで確認された崩れ（短い語句のみ）
        ("表面が銅箔卵か", "表面が、同博覧会"),
        ("同白覧会", "同博覧会"),
        ("日本現在の減産の植物", "日本の原産植物"),
        ("合わせ総盟", "為替相場"),
        ("川瀬長岡", "為替相場"),
        ("出線と大学長官", "ベッセント財務長官"),
        ("園の川瀬市場", "円の為替市場"),
        ("開入する際", "介入する際"),
        ("日本や日園", "日本や日銀"),
        ("政策立案者側と", "政策立案者がどう動くかを"),
        ("大金に詳しく企画している", "かなり詳しく把握している"),
        ("ベッセントス", "ベッセント財務長官"),
        ("独立性が起こっておかれるべき", "独立性が確保されるべき"),
        ("政策統計", "政策当局"),
        ("大盟会長", "ダイモン会長"),
    ],
    "EcNJBAsZgY8": [
        ("朝代委員", "浅田委員"),
        ("生存食用の属、消費者物価", "生鮮食品を除く消費者物価"),
        ("生存食用の属", "生鮮食品を除く"),
        ("2%をしたまわる", "2％を下回る"),
        ("経済の状況も患者も強くは強いとは言えない", "経済の状況も必ずしも強いとは言えない"),
    ],
    "mzklpHRM4m8": [
        ("生田数で決定しました", "賛成多数で決定しました"),
        ("内容を完結にする", "内容を簡潔にする"),
        ("市会社から指名", "司会者から指名"),
        ("拒守したまま", "挙手したまま"),
        ("政策禁理", "政策金利"),
        ("オタンプコールレート、終わらないともの", "無担保コールレート（オーバーナイト物）の"),
        ("保管等材料金制度", "補完当座預金制度"),
        ("適用値率", "適用利率"),
        ("基準化し付け値率", "基準貸付利率"),
        ("朝代委員", "浅田委員"),
        ("生成食品を除く", "生鮮食品を除く"),
        ("患者も強くは強いとは", "必ずしも強いとは"),
        ("市場調整、金融市場調整創新を据え起く", "金融市場調節方針を据え置く"),
        ("このタイミングでの売り上げ", "このタイミングでの利上げ"),
        ("気候変度対オープニッシュ", "気候変動対応オペ"),
        ("金融調節の円滑な上", "金融調節の円滑な運営"),
        ("休関点から貸付権利", "観点から貸付金利"),
        ("全日で決定", "全員一致で決定"),
        ("枠の景気", "わが国の景気"),
        ("さっき行き", "先行き"),
        ("各種製作等", "各種政策等"),
        ("経済を下座さえ", "経済を下支え"),
        ("かわせへんやす", "為替円安"),
        ("前半日で高いのみ", "前年比で高い伸び"),
        ("核上昇発力", "価格上昇圧力"),
        ("吐き押し始め", "波及し始め"),
        ("地域上昇", "賃金上昇"),
        ("販売核への転換", "販売価格への転嫁"),
        ("プラスサブ", "プラス幅"),
        ("予想ぶっかじょう 産業省率", "予想物価上昇率"),
        ("貴重的な上昇率", "基調的な上昇率"),
        ("ワークニーの経済物価", "わが国の経済・物価"),
        ("大胸沿って推じ", "概ね沿って推移"),
        ("リアキペース", "利上げペース"),
        ("タイミングレート", "ターミナルレート"),
        ("中立近利", "中立金利"),
        ("春刀", "春闘"),
        ("ベッセント長官との改弾", "ベッセント長官との会談"),
        ("日本の長勤理", "日本の長期金利"),
        ("延安", "円安"),
        ("高田町議員", "高田審議委員"),
        ("ボードメーバー", "ボードメンバー"),
        ("行為形成", "合意形成"),
        ("カワセレット", "為替レート"),
        ("集焼技術格差", "金利差"),
        ("ご対策ください", "ご退席ください"),
    ],
    "TuI4euaLfuE": [
        ("飲食利用品消費成立", "飲食料品消費税率"),
        ("修行者負担軽減支援金", "就業者負担軽減支援金"),
        ("本対抗", "本大綱"),
        ("コンパンの対抗", "今般の大綱"),
        ("閣府議決定", "閣議決定"),
        ("飲食料費に係る消費成立", "飲食料品に係る消費税率"),
        ("医療費に係る消費税", "飲食料品に係る消費税"),
        ("本速課税事業者", "免税事業者が課税事業者"),
        ("農林業業者", "農林漁業者"),
        ("個別の売上だか", "個別の売上高"),
        ("資金振り支援", "資金繰り支援"),
        ("予算編成課程", "予算編成過程"),
        ("特例交際", "特例公債"),
        ("特例交差", "特例公債"),
        ("素材特別措置", "租税特別措置"),
        ("再出及び再入", "歳出及び歳入"),
        ("区議において", "閣議において"),
        ("胸のご発言", "旨の御発言"),
        ("八方自治体", "地方自治体"),
        ("周知候補", "周知、広報"),
        ("第3時に国間通貨スワープ", "第3次二国間通貨スワップ"),
        ("金融協力の進化", "金融協力の深化"),
        ("ディザイム省", "財務省"),
        ("市場の不信任", "市場の信認"),
        ("市場の新人", "市場の信認"),
        ("万全を築く", "万全を期す"),
        ("卓球的速やかにこうする", "可及的速やかに講ずる"),
        ("定年に望んで", "丁寧に臨んで"),
        ("食料費用消費減税", "食料品の消費税減税"),
        ("サイモザンダ化の大GDPG", "債務残高の対GDP比"),
        ("市場の森林確保", "市場の信認確保"),
        ("採出、採入、両面", "歳出・歳入両面"),
        ("再出再入道弁", "歳出・歳入両面"),
        ("税収同行", "税収動向"),
        ("対GDP比を安定的に仕上げていく", "対GDP比を安定的に引き下げていく"),
        ("財政規模この中", "財政規模、この中"),
        ("特に応じたきめ細かな給付", "所得に応じたきめ細かな給付"),
        ("消費税率引き下げの実習", "消費税率引下げの実施"),
        ("歳出及び歳入前般", "歳出及び歳入全般"),
        ("史上の信任", "市場の信認"),
        ("素税特別措置", "租税特別措置"),
        ("真に金融性の高い", "真に緊要性の高い"),
        ("同博の予算", "多額の予算"),
        ("当生予算", "補正予算"),
        ("予算編成プレッセス", "予算編成プロセス"),
        ("財務残高の対策、 GDP", "債務残高の対GDP比"),
        ("国際発行額", "国債発行額"),
        ("受税県税の再現", "消費税減税の財源"),
        ("不可持国債", "赤字国債"),
        ("資金が退留", "資金が滞留"),
        ("全部見仕上げる", "全部召し上げる"),
        ("法律業界への手術", "小売業界への周知"),
        ("総務省とか計算省", "総務省とか経産省"),
        ("根下げ", "値下げ"),
        ("沖縄進行予算", "沖縄振興予算"),
        ("挽のせ", "上乗せ"),
        ("さつびら", "札びら"),
        ("処置しておりません", "承知しておりません"),
        # 7.4.15と比較用文字起こしの照合で確認した崩れ。
        ("関する対抗", "関する大綱"),
        ("政策改正対抗", "政策改正大綱"),
        ("税制改正対抗", "税制改正大綱"),
        ("対抗に基づいて", "大綱に基づいて"),
        ("今日、対抗で", "今日の大綱で"),
        ("通貨スワップ契約に証明", "通貨スワップ契約に署名"),
        ("協力分析", "協力覚書"),
        ("金融協力の深化にする", "金融協力の深化に資する"),
        ("食料品の消費税減税の大きな話もらいました", "食料品の消費税減税について、今、大臣からお話がありました"),
        ("財源の確保について見方います", "財源の確保について伺います"),
        ("このコッシー", "この骨子"),
        ("さゆる見直し", "あらゆる見直し"),
        ("財政規模というものを制裁", "財政規模というものを精査"),
        ("市場の信認確保に配置", "市場の信認確保に配意"),
        ("さらなる再入確保", "さらなる歳入確保"),
        ("形状してきている", "計上してきている"),
        ("ふっかたが対策", "物価高対策"),
        ("予算衛生改革", "予算編成改革"),
        ("自己要求", "事項要求"),
        ("様々な特化への活用", "様々な特会の活用"),
        ("補助金、外国の見直し", "補助金等の見直し"),
        ("適切に適切な周知", "適宜、適切な周知"),
        ("まほとり申し訳ございません", "誠に申し訳ございません"),
        ("県内にある消費税率", "飲食料品に係る消費税率"),
        ("速やかに返す", "速やかに開始する"),
        ("市場の信任", "市場の信認"),
        ("総額表示義務の 消費税率の引き下げ", "総額表示義務の特例などを設けることとしています。消費税率の引き下げ"),
        ("事業者等への広報活動を受けます。", "事業者等への広報活動を"),
        ("活動を速やかに開始するとともに", "速やかに開始するとともに"),
        ("これらの取組が、両国の貿易投資関係の更なる発展や、金融協力の深化につながるとともに、現地通貨", "現地通貨"),
        ("施策の着実な具体化。 今後は、実行に取り組んで", "施策の着実な具体化・実行に取り組んで"),
        ("長期経費が一応上昇", "長期金利が一部上昇"),
        ("あらかみで", "改めて"),
        ("防衛費が増殖", "防衛費が増額"),
        ("財源が、財政の影響論のような感じか教えてください", "財源や財政への影響をどのようにお考えでしょうか"),
        ("その合言について", "その報道について"),
        ("それを防発", "それを暴発"),
        ("安控とロール", "アンコントロール"),
        ("通念の 国債発行額", "通年の国債発行額"),
        ("範囲するレベル", "配慮するレベル"),
        ("数年の国債発行額", "通年の国債発行額"),
        ("修行者、負担、軽減支援金", "就業者負担軽減支援金"),
        # 7.4.17の40秒認識テストで確認した崩れ。
        ("それで、誰に冒頭行わせられませんでしょうか", "それでは大臣、冒頭、発言ありますでしょうか"),
        ("先の主員選", "先の衆院選"),
        ("ぶっかだ化対策", "物価高対策"),
        ("含みの期待", "国民の期待"),
        ("副国された一項", "確認された一方"),
        ("約半年におよそ副国会議", "約半年にわたる国民会議"),
        ("議論が行っていなが", "議論を経ても、なお"),
        ("政策公開の疑問", "政策効果への疑問"),
        ("農業や外食産業などの悪意", "農業や外食産業などへの悪影響"),
        ("税率を持つことは、戻すことは", "また2年間の税率引下げを元に戻すことは"),
        ("税制改正大綱の確立決定は行われました", "大綱を閣議決定されました"),
        ("日曜日で1時の時", "野党系知事の時"),
        ("自民党刑後 これを知事に変わったときに", "自民党系の知事に変わった途端に"),
        ("同額するというのは", "増額するというのは"),
        ("サベス的、大量", "差別的な対応"),
        ("大臣は考えにならない", "大臣はお考えにならない"),
        ("一般のでもいい", "一般論でもいい"),
        ("雇計の知事", "野党系の知事"),
        ("上乗せ下がれる", "増額される"),
        ("問題あると対応", "問題のある対応"),
        ("県民支援分の田中", "読売新聞の田中"),
        ("小遺跡の財政の話", "消費税減税の話"),
        ("受税県税の財源", "財源"),
        ("大陸の示される時期", "具体的に示される時期"),
        ("当たられてお伺い", "改めて伺い"),
        ("赤字国債に対らない", "赤字国債には頼らない"),
        ("赤字国債は遅くなってあれば", "赤字国債を発行するのであれば"),
        ("意見も一致でございます", "意見も一部にございます"),
        ("万全を起す", "万全を期す"),
        ("資金グリ支援", "資金繰り支援"),
        ("支援内容を舞台化", "支援内容を具体化"),
        # 財務省公式会見概要（令和8年9月15日）との照合で確認。
        ("飲食料費に係る", "飲食料品に係る"),
        ("広報活動を受けられます", "広報活動を速やかに開始します"),
        ("活動を速やかに回収", "速やかに開始"),
        ("第3時に国管通貨スワップ", "第3次二国間通貨スワップ"),
        ("協力を具合書", "協力覚書"),
        ("事態務省", "財務省"),
        ("金融協力の深化にする", "金融協力の深化に資する"),
        ("今般の対抗", "今般の大綱"),
        ("市場の審認", "市場の信認"),
        ("法案に守り込んで", "法案に盛り込んで"),
        ("丁寧に望んで", "丁寧に臨んで"),
        ("修業者負担軽減支援金", "就業者負担軽減支援金"),
        ("このことを 生まれて", "このことを踏まえて"),
        ("避散文書", "3文書"),
        ("その合同について", "その報道について"),
        ("お答えできたねます", "お答えできかねます"),
        ("適当的説に", "適宜適切に"),
        ("査定化されて", "査定がされて"),
        ("寝下げ", "値下げ"),
    ],
}

def load_config():
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        return DEFAULT_CONFIG.copy()
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    out = DEFAULT_CONFIG.copy()
    out.update(cfg)

    # V6以前の設定を初めてV7へ移行するときは、英語会見監視を再開する。
    # V7で生成・保存した後は、利用者が設定した値をそのまま尊重する。
    if "openai_translation_enabled" not in cfg:
        out["enable_bessent_monitoring"] = True

    # 旧config.jsonをそのまま使っても、新しい対象者・予定確認先を失わない。
    for key in (
        "search_terms", "event_schedule_urls", "event_keywords",
        "organization_channel_terms",
    ):
        merged = []
        for value in list(DEFAULT_CONFIG.get(key, [])) + list(cfg.get(key, [])):
            # 6.1.0で誤って追加した、実在しない米財務省URLは移行時に除去。
            if value == "https://home.treasury.gov/news/press-information":
                continue
            if value not in merged:
                merged.append(value)
        out[key] = merged

    default_queries = str(DEFAULT_CONFIG["search_query"]).split("|")
    custom_queries = str(cfg.get("search_query", "")).split("|")
    out["search_query"] = "|".join(
        dict.fromkeys(x.strip() for x in custom_queries + default_queries if x.strip())
    )
    channels = dict(DEFAULT_CONFIG.get("priority_channel_ids", {}))
    channels.update(cfg.get("priority_channel_ids", {}))
    out["priority_channel_ids"] = channels
    # 旧config.jsonの辞書で、新版に追加した対象者・動画設定を消さない。
    for key in (
        "archive_official_speakers", "archive_speaker_ranges",
        "archive_official_reference_urls",
    ):
        merged_mapping = dict(DEFAULT_CONFIG.get(key, {}))
        merged_mapping.update(cfg.get(key, {}))
        out[key] = merged_mapping
    return out


CONFIG = load_config()

# 配信中の文字起こしを、監視プログラムを閉じずに手動停止するための管理表。
ACTIVE_TRANSCRIPTIONS = {}
ACTIVE_TRANSCRIPTIONS_LOCK = threading.Lock()
# 予約LIVE前に発見した財務省・日銀の公式本文。LIVE処理中も更新を参照する。
LIVE_OFFICIAL_REFERENCES = {}
LIVE_OFFICIAL_REFERENCES_LOCK = threading.Lock()
OFFICIAL_REFERENCE_WATCHERS = set()
OFFICIAL_REFERENCE_WATCHERS_LOCK = threading.Lock()
EVENT_SCHEDULE_CACHE = {
    "checked_at": 0.0,
    "date": "",
    "active": False,
    "matches": [],
}


class YouTubeQuotaExceeded(RuntimeError):
    """YouTube Data API の1日検索クォータを使い切った。"""


def effective_scan_interval_minutes():
    """search.list の1日100回制限を超えない安全な検索間隔。"""
    try:
        requested = float(CONFIG.get("scan_interval_minutes", 20))
    except (TypeError, ValueError):
        requested = 20
    return max(20.0, requested)


def seconds_until_safe_quota_retry():
    """米国太平洋時間の日次更新後となる次の08:05 UTCまで待つ。

    太平洋時間は夏時間中UTC-7、標準時間中UTC-8。08:05 UTCなら
    どちらの期間でも日付更新後なので、WindowsのタイムゾーンDBに依存しない。
    """
    now = datetime.now(timezone.utc)
    retry_at = now.replace(hour=8, minute=5, second=0, microsecond=0)
    if retry_at <= now:
        retry_at += timedelta(days=1)
    return max(300, int((retry_at - now).total_seconds()))


def find_ffmpeg():
    candidates = [
        APP_DIR / "ffmpeg" / "bin" / "ffmpeg.exe",
        APP_DIR / "ffmpeg.exe",
    ]
    for p in candidates:
        if p.exists():
            return str(p)

    import shutil
    found = shutil.which("ffmpeg")
    if found:
        return found

    raise FileNotFoundError(
        "FFmpegが見つかりません。\n"
        "このフォルダの ffmpeg\\bin\\ffmpeg.exe にFFmpegを配置するか、"
        "setup_ffmpeg.bat を実行してください。"
    )


def notify(title, message):
    print(f"\n🔔 {title}\n{message}\n")
    if notification:
        try:
            notification.notify(
                title=title,
                message=message,
                app_name="YouTube会見監視",
                timeout=10
            )
        except Exception:
            pass


def manual_stop_listener():
    """コンソールで s + Enter を受け取り、実行中の文字起こしだけを止める。"""
    while True:
        try:
            command = input().strip().lower()
        except (EOFError, OSError):
            return
        except KeyboardInterrupt:
            return

        if command != "s":
            continue

        with ACTIVE_TRANSCRIPTIONS_LOCK:
            running = list(ACTIVE_TRANSCRIPTIONS.items())

        if not running:
            print("ℹ️ 現在、停止できる文字起こしはありません。")
            continue

        for video_id, control in running:
            control["manual_stop"].set()
            control["stop"].set()
            print(f"⏹ 手動停止を受け付けました: {control['title']} ({video_id})")
        print("   Live監視はそのまま継続します。")


def api_get(endpoint, params):
    p = dict(params)
    p["key"] = CONFIG["youtube_api_key"]
    r = requests.get(
        f"https://www.googleapis.com/youtube/v3/{endpoint}",
        params=p,
        timeout=20
    )
    if r.status_code == 429:
        try:
            payload = r.json()
            reasons = {
                x.get("reason", "")
                for x in payload.get("error", {}).get("errors", [])
            }
            message = payload.get("error", {}).get("message", r.text[:500])
        except Exception:
            reasons = set()
            message = r.text[:500]
        if "rateLimitExceeded" in reasons or "quota" in message.lower():
            raise YouTubeQuotaExceeded(message)
    if not r.ok:
        raise RuntimeError(f"YouTube API {r.status_code}: {r.text[:500]}")
    return r.json()


def visible_page_text(raw_html):
    """外部ライブラリを増やさず、予定ページを検索しやすい文字列にする。"""
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", raw_html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def event_date_patterns(day):
    months = (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December"
    )
    month_name = months[day.month - 1]
    return [
        day.isoformat(),
        f"{day.year}/{day.month}/{day.day}",
        f"{day.month}/{day.day}",
        f"{day.month}月{day.day}日",
        f"{month_name} {day.day}, {day.year}",
        f"{day.day} {month_name} {day.year}",
    ]


def page_event_matches(text, day):
    """日付の近くに国際金融イベント語がある箇所を返す。"""
    lowered = text.lower()
    keywords = [str(x) for x in CONFIG.get("event_keywords", []) if str(x).strip()]
    matches = []
    for date_text in event_date_patterns(day):
        start = 0
        date_lower = date_text.lower()
        while True:
            pos = lowered.find(date_lower, start)
            if pos < 0:
                break
            window = text[max(0, pos - 350): min(len(text), pos + len(date_text) + 350)]
            if any(keyword.lower() in window.lower() for keyword in keywords):
                matches.append(re.sub(r"\s+", " ", window).strip()[:300])
            start = pos + len(date_lower)
    return matches


def is_finance_event_day(force=False):
    """財務省・G20・日銀の公式予定から、日本時間のイベント日を判定する。"""
    jst = timezone(timedelta(hours=9))
    today = datetime.now(jst).date()
    today_text = today.isoformat()
    try:
        refresh_seconds = max(
            3600, float(CONFIG.get("event_schedule_check_hours", 6)) * 3600
        )
    except (TypeError, ValueError):
        refresh_seconds = 6 * 3600

    now_mono = time.monotonic()
    if (
        not force
        and EVENT_SCHEDULE_CACHE["date"] == today_text
        and now_mono - EVENT_SCHEDULE_CACHE["checked_at"] < refresh_seconds
    ):
        return EVENT_SCHEDULE_CACHE["active"]

    manual_dates = {str(x).strip() for x in CONFIG.get("manual_event_dates", [])}
    matches = ["config.json の manual_event_dates"] if today_text in manual_dates else []

    for url in CONFIG.get("event_schedule_urls", []):
        try:
            response = requests.get(
                str(url), timeout=20,
                headers={"User-Agent": "Mozilla/5.0 YouTubeConferenceMonitor/1.0"}
            )
            response.raise_for_status()
            page_matches = page_event_matches(visible_page_text(response.text), today)
            if page_matches:
                matches.append(str(url))
        except Exception as e:
            print(f"⚠️ イベント予定の確認失敗: {url} ({e})")

    EVENT_SCHEDULE_CACHE.update({
        "checked_at": now_mono,
        "date": today_text,
        "active": bool(matches),
        "matches": matches,
    })
    if matches:
        print(f"🌐 国際金融イベント日を検出: {today_text}")
        for source in matches:
            print(f"   確認元: {source}")
    else:
        print(f"📅 国際金融イベント予定なし: {today_text}")
    return bool(matches)


def search_event(event_type):
    """対象者を1回のOR検索でまとめ、Live種別を限定して検索する。

    旧版は検索語10個ごとにsearch.listを呼んでいたため、5分間隔で
    最大2,880回/日になっていた。通常投稿動画は検索対象に含めない。
    """
    if event_type not in ("live", "upcoming"):
        raise ValueError(f"未対応のLive種別です: {event_type}")
    found = {}
    q = str(CONFIG.get("search_query", "")).strip()
    if not q:
        q = str(DEFAULT_CONFIG["search_query"])
    if not CONFIG.get("enable_bessent_monitoring", False):
        q = "|".join(
            term for term in q.split("|")
            if not re.search(r"Bessent|ベッセント", term, re.I)
        )
    data = api_get("search", {
        "part": "snippet",
        "q": q,
        "type": "video",
        "eventType": event_type,
        "order": "date",
        "maxResults": 25,
        "regionCode": CONFIG["region_code"],
        "relevanceLanguage": CONFIG["language"]
    })
    for item in data.get("items", []):
        vid = item.get("id", {}).get("videoId")
        if vid:
            found[vid] = item
    return list(found.values())


def search_live():
    return search_event("live")


def search_upcoming():
    return search_event("upcoming")


def scheduled_start_for_video(video_id):
    """予約LIVEの開始予定時刻をYouTube videos.listから取得する。"""
    try:
        data = api_get("videos", {
            "part": "liveStreamingDetails",
            "id": video_id,
        })
        items = data.get("items", [])
        value = str(
            items[0].get("liveStreamingDetails", {}).get("scheduledStartTime", "")
            if items else ""
        ).strip()
        if not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception as e:
        print(f"⚠️ 予約LIVE開始時刻を取得できません ({video_id}): {e}")
        return None


def _html_to_plain(source):
    source = re.sub(r"(?is)<(?:script|style).*?>.*?</(?:script|style)>", " ", source)
    source = html.unescape(re.sub(r"(?s)<[^>]+>", "\n", source))
    source = re.sub(r"[\t\r ]+", " ", source)
    return re.sub(r"\n+", "\n", source).strip()


def _decode_official_html(response):
    """官公庁ページを、宣言漏れや誤ったcharsetがあっても日本語で復元する。"""
    raw = response.content
    declared = str(getattr(response, "encoding", "") or "").strip()
    apparent = str(getattr(response, "apparent_encoding", "") or "").strip()
    encodings = ["utf-8-sig", declared, apparent, "cp932", "shift_jis", "euc_jp"]
    candidates = []
    used = set()
    for encoding in encodings:
        key = encoding.lower().replace("_", "-") if encoding else ""
        if not key or key in used:
            continue
        used.add(key)
        try:
            decoded = raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
        score = sum(
            decoded.count(marker) * weight
            for marker, weight in (
                ("冒頭発言", 100), ("質疑応答", 80), ("（以上）", 60),
                ("財務省", 20), ("記者会見", 20),
            )
        )
        score += len(re.findall(r"[ぁ-んァ-ヶ一-龠]", decoded[:50000]))
        candidates.append((score, decoded, encoding))
    if not candidates:
        raise RuntimeError("公式ページの文字コードを判定できません")
    return max(candidates, key=lambda item: item[0])[1]


def _official_error_summary(error):
    """公式資料取得は、利用者が原因を判断できる範囲で詳細を表示する。"""
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status:
        return f"HTTP {status} ({type(error).__name__})"
    detail = re.sub(r"https?://\S+", "[URL]", str(error)).strip()
    if detail:
        return f"{type(error).__name__}: {detail[:240]}"
    return type(error).__name__


def _html_links(source, base_url):
    links = []
    for match in re.finditer(
        r"(?is)<a\b[^>]*href\s*=\s*['\"]([^'\"]+)['\"][^>]*>(.*?)</a>",
        source,
    ):
        href = urljoin(base_url, html.unescape(match.group(1)).strip())
        title = re.sub(r"\s+", " ", _html_to_plain(match.group(2))).strip()
        if href and title:
            links.append((href, title))
    return links


def _html_links_with_context(source, base_url):
    """リンク直前の日付表示も含めて日銀新着情報を判定する。"""
    links = []
    for match in re.finditer(
        r"(?is)<a\b[^>]*href\s*=\s*['\"]([^'\"]+)['\"][^>]*>(.*?)</a>",
        source,
    ):
        href = urljoin(base_url, html.unescape(match.group(1)).strip())
        title = re.sub(r"\s+", " ", _html_to_plain(match.group(2))).strip()
        context = re.sub(
            r"\s+", " ", _html_to_plain(source[max(0, match.start() - 240):match.start()])
        ).strip()
        if href and title:
            links.append((href, title, context[-120:]))
    return links


def _extract_pdf_text(content):
    """pypdfが利用可能な環境では日銀PDF本文も補正資料にする。"""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception:
        return ""


def discover_live_official_reference(person, scheduled_start):
    """当日公開された財務省会見概要または日銀資料を探す。"""
    sources = CONFIG.get("official_live_source_urls", {})
    if "日銀" in person or "日本銀行" in person:
        kind = "boj"
    elif any(x in person for x in ("財務大臣", "財務官", "片山", "三村")):
        kind = "mof"
    else:
        return "", ""

    index_url = str(sources.get(kind, "")).strip()
    if not index_url:
        return "", ""
    response = requests.get(index_url, timeout=(5, 20), headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    links = _html_links(_decode_official_html(response), index_url)
    jst = timezone(timedelta(hours=9))
    target = (scheduled_start or datetime.now(timezone.utc)).astimezone(jst)
    ymd = target.strftime("%Y%m%d")
    md_terms = (f"{target.month}/{target.day}", f"{target.month}月{target.day}日")

    if kind == "mof":
        candidates = [
            (href, title) for href, title in links
            if "/public_relations/conference/" in href
            and href.endswith(".html")
            and (ymd in href or any(term in title for term in md_terms))
        ]
        for href, title in candidates[:5]:
            detail = requests.get(href, timeout=(5, 20), headers={"User-Agent": "Mozilla/5.0"})
            detail.raise_for_status()
            plain = _html_to_plain(_decode_official_html(detail))
            start = plain.find("冒頭発言")
            end = plain.find("（以上）", start)
            if start >= 0:
                if end < 0:
                    end = min(len(plain), start + 20000)
                body = plain[start:end].strip()
                if len(body) >= 200:
                    return body, href
        return "", ""

    keywords = (
        "金融市場調節方針", "金融政策決定会合", "総裁記者会見",
        "記者会見", "展望レポート", "補完当座預金制度", "金利",
    )
    candidates = []
    for href, title, context in _html_links_with_context(response.text, index_url):
        if not any(term in f"{context} {title}" for term in md_terms):
            continue
        if any(keyword in title for keyword in keywords):
            candidates.append((href, title))
    gathered = []
    used_urls = []
    for href, title in candidates[:8]:
        if href in used_urls:
            continue
        used_urls.append(href)
        try:
            detail = requests.get(href, timeout=(5, 25), headers={"User-Agent": "Mozilla/5.0"})
            detail.raise_for_status()
            content_type = str(detail.headers.get("Content-Type", "")).lower()
            if href.lower().endswith(".pdf") or "application/pdf" in content_type:
                body = _extract_pdf_text(detail.content)
            else:
                body = _html_to_plain(_decode_official_html(detail))
            gathered.append(f"{title}\n{body[:12000]}".strip())
        except Exception:
            gathered.append(title)
    reference = "\n\n".join(x for x in gathered if x).strip()
    return (reference, index_url) if len(reference) >= 30 else ("", "")


def official_reference_watcher(video_id, person, title, scheduled_start):
    """開始5分前から公式ページを1分間隔で確認し、LIVE補正へ渡す。"""
    try:
        before = max(0.0, float(CONFIG.get("official_pre_live_monitor_minutes", 5)))
        after = max(1.0, float(CONFIG.get("official_pre_live_monitor_after_start_minutes", 10)))
        interval = max(30, int(CONFIG.get("official_pre_live_monitor_interval_seconds", 60)))
        start_at = (scheduled_start or datetime.now(timezone.utc)) - timedelta(minutes=before)
        finish_at = (scheduled_start or datetime.now(timezone.utc)) + timedelta(minutes=after)
        while datetime.now(timezone.utc) < start_at:
            time.sleep(min(30, max(1, int((start_at - datetime.now(timezone.utc)).total_seconds()))))
        print(f"🏛 公式資料の事前監視開始: {person}（開始{before:g}分前から）")
        previous = ""
        while datetime.now(timezone.utc) <= finish_at:
            try:
                reference, source_url = discover_live_official_reference(person, scheduled_start)
                if reference and reference != previous:
                    previous = reference
                    with LIVE_OFFICIAL_REFERENCES_LOCK:
                        LIVE_OFFICIAL_REFERENCES[video_id] = {
                            "text": reference,
                            "url": source_url,
                            "updated_at": datetime.now().isoformat(timespec="seconds"),
                        }
                    print(f"🏛 公式資料を検出・補正に使用: {source_url}（{len(reference)}文字）")
            except Exception as e:
                print(f"⚠️ 公式資料の事前監視エラー: {_official_error_summary(e)}")
            time.sleep(interval)
    finally:
        print(f"🏛 公式資料の事前監視終了: {person}")


def ensure_official_reference_watcher(video_id, person, title, scheduled_start=None):
    if not CONFIG.get("official_pre_live_monitor_enabled", True):
        return
    if not any(x in person for x in ("財務大臣", "財務官", "片山", "三村", "日銀", "日本銀行")):
        return
    with OFFICIAL_REFERENCE_WATCHERS_LOCK:
        if video_id in OFFICIAL_REFERENCE_WATCHERS:
            return
        OFFICIAL_REFERENCE_WATCHERS.add(video_id)
    threading.Thread(
        target=official_reference_watcher,
        args=(video_id, person, title, scheduled_start),
        daemon=True,
    ).start()


def youtube_watch_is_live(video_id):
    """検索APIを使わず、動画ページの現在Liveフラグを確認する。"""
    try:
        response = requests.get(
            f"https://www.youtube.com/watch?v={video_id}",
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        return (
            '"isLive":true' in response.text
            and '"isLiveContent":true' in response.text
        )
    except Exception as e:
        print(f"⚠️ Live状態確認失敗 ({video_id}): {e}")
        return False


def priority_feed_items(channel_name, channel_id):
    """YouTube公式Atomフィードから対象者を含む最近の動画だけを返す。"""
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    response = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    root = ET.fromstring(response.content)
    atom = "{http://www.w3.org/2005/Atom}"
    yt = "{http://www.youtube.com/xml/schemas/2015}"
    items = []
    for entry in root.findall(f"{atom}entry"):
        video_node = entry.find(f"{yt}videoId")
        title_node = entry.find(f"{atom}title")
        if video_node is None or title_node is None:
            continue
        video_id = (video_node.text or "").strip()
        title = (title_node.text or "").strip()
        if not video_id or not person_for(title, channel_name):
            continue
        if is_excluded_broadcast(title):
            continue
        items.append({
            "id": {"videoId": video_id},
            "snippet": {
                "title": title,
                "description": channel_name,
                "channelTitle": channel_name,
            },
        })
    return items


def priority_channel_watcher(active, processed):
    """主要海外チャンネルを2分間隔で補助監視し、検索漏れを拾う。"""
    try:
        interval = max(1.0, float(CONFIG.get("priority_feed_interval_minutes", 2)))
    except (TypeError, ValueError):
        interval = 2.0

    print(f"📡 海外主要4チャンネル補助監視: {interval:g}分間隔（APIクォータ消費なし）")
    while True:
        for channel_name, channel_id in CONFIG.get("priority_channel_ids", {}).items():
            try:
                for item in priority_feed_items(channel_name, channel_id):
                    video_id = item["id"]["videoId"]
                    if video_id in active or video_id in processed:
                        continue
                    if youtube_watch_is_live(video_id):
                        print(f"📡 チャンネル直接監視でLive検出: {channel_name}")
                        threading.Thread(
                            target=process_conference,
                            args=(item, active, processed, "live"),
                            daemon=True,
                        ).start()
            except Exception as e:
                print(f"⚠️ チャンネル補助監視エラー: {channel_name} ({e})")
        time.sleep(int(interval * 60))


def person_for(title, description):
    t = re.sub(r"\s+", " ", title + " " + description)
    if re.search(r"\b(?:Scott\s+)?Bessent\b", t, re.I) or "ベッセント" in t:
        if not CONFIG.get("enable_bessent_monitoring", False):
            return None
        return "スコット・ベッセント 米財務長官"
    if "高市" in t:
        return "高市総理"
    if "片山" in t and ("財務" in t or "大臣" in t):
        return "片山財務大臣"
    if "三村" in t and ("財務" in t or "財務官" in t):
        return "三村財務官"
    boj_members = [
        (("植田和男", "植田総裁", "日銀総裁"), "植田和男 日銀総裁"),
        (("内田眞一", "内田副総裁"), "内田眞一 日銀副総裁"),
        (("氷見野良三", "氷見野副総裁"), "氷見野良三 日銀副総裁"),
        (("高田創", "高田審議委員"), "高田創 日銀審議委員"),
        (("田村直樹", "田村審議委員"), "田村直樹 日銀審議委員"),
        (("小枝淳子", "小枝審議委員"), "小枝淳子 日銀審議委員"),
        (("増一行", "増審議委員"), "増一行 日銀審議委員"),
        (("浅田統一郎", "浅田審議委員"), "浅田統一郎 日銀審議委員"),
        (("佐藤綾野", "佐藤審議委員"), "佐藤綾野 日銀審議委員"),
    ]
    for aliases, label in boj_members:
        if any(alias in t for alias in aliases):
            return label
    return None


def is_organization_channel(item):
    """検索結果のチャンネル名が登録済みの組織名か確認する。"""
    snippet = item.get("snippet", {})
    channel_title = str(snippet.get("channelTitle", "")).strip()
    if not channel_title:
        return False
    normalized = channel_title.casefold()
    return any(
        str(term).casefold() in normalized
        for term in CONFIG.get("organization_channel_terms", [])
        if str(term).strip()
    )


def is_excluded_broadcast(title):
    """本当の生中継を誤除外しないよう、説明欄ではなくタイトルだけで判定する。"""
    compact_title = re.sub(r"\s+", "", str(title)).lower()
    terms = CONFIG.get("excluded_title_terms", ["見逃し配信", "見逃しライブ"])
    return any(
        re.sub(r"\s+", "", str(term)).lower() in compact_title
        for term in terms
        if str(term).strip()
    )


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|]+', "_", s)
    return re.sub(r"\s+", " ", s).strip()[:100] or "conference"


def fmt(sec):
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def parse_hms(value):
    parts = [int(x) for x in str(value).split(":")]
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = 0, parts[0], parts[1]
    else:
        return 0.0
    return float(h * 3600 + m * 60 + s)


def normalize_text(text, video_id=None):
    text = re.sub(r"\s+", " ", text).strip()
    for wrong, correct in TEXT_CORRECTIONS:
        text = text.replace(wrong, correct)
    if video_id:
        for wrong, correct in VIDEO_TEXT_CORRECTIONS.get(video_id, []):
            text = text.replace(wrong, correct)

    # チャンク境界で分断された「人数 / 速な」を「迅速な」へ。
    text = re.sub(r"人数\s*速な", "迅速な", text)
    text = re.sub(r"人\s*速な", "迅速な", text)

    # よく起きる句読点なしの連結を補正。
    text = text.replace("高騰する中国民", "高騰する中、国民")
    return text


def repetition_score(text):
    """同一語・短句の異常反復を検出する。0.0=正常、1.0に近いほど異常。"""
    t = re.sub(r"[\s、。！？!?]+", " ", text).strip()
    if not t:
        return 0.0
    tokens = [x for x in t.split(" ") if x]
    if len(tokens) >= 6:
        from collections import Counter
        c = Counter(tokens)
        most = c.most_common(1)[0][1]
        if most >= 4:
            return most / len(tokens)
    # 「大胆に、大胆に…」のような句読点区切り反復も検出
    parts = [x.strip() for x in re.split(r"[、,。]+", text) if x.strip()]
    if len(parts) >= 5:
        from collections import Counter
        c = Counter(parts)
        most = c.most_common(1)[0][1]
        if most >= 4:
            return most / len(parts)
    return 0.0


def looks_hallucinated(text):
    # 英語の長い文や二文一組の反復も検出する。
    sentences = [re.sub(r"\s+", " ", x).strip().casefold()
                 for x in re.split(r"[。！？.!?]+", text) if x.strip()]
    from collections import Counter
    counts = Counter(x for x in sentences if len(x) >= 20)
    if any(count >= 4 for count in counts.values()):
        return True
    # 「I'm a heart surgeon.」等、20文字未満の英文反復も検出。
    short_counts = Counter(x for x in sentences if len(x) >= 8 and len(x.split()) >= 3)
    if any(count >= 6 for count in short_counts.values()):
        return True
    score = repetition_score(text)
    if score >= 0.45:
        return True
    # 同じ2～8文字の語句が4回以上連続
    if re.search(r"(.{2,8}?)(?:[、,\s]*\1){3,}", text):
        return True
    return False


_TRANSLATION_COOLDOWN_UNTIL = {}
_BING_TRANSLATOR_STATE = {}
_OPENAI_USAGE_LOCK = threading.Lock()
_POST_ASSIST_FILE_LOCK = threading.Lock()
POST_ASSIST_QUEUE = queue.Queue()
POST_ASSIST_STATUS_QUEUE = queue.Queue()


def load_local_env():
    """同じフォルダの.envを読み込む。既存の環境変数は上書きしない。"""
    path = APP_DIR / ".env"
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception as e:
        print(f"⚠️ .envを読めません: {e}")


load_local_env()


def openai_usage_path():
    return DATA_DIR / f"openai_usage_{datetime.now():%Y-%m}.json"


def load_openai_usage():
    path = openai_usage_path()
    default = {
        "month": datetime.now().strftime("%Y-%m"),
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost_usd": 0.0,
        "estimated_cost_jpy": 0.0,
        "requests": 0,
    }
    if not path.exists():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        default.update(value)
    except Exception as e:
        print(f"⚠️ OpenAI使用量記録を読めません。安全のため翻訳を停止します: {e}")
        default["blocked"] = True
    return default


def openai_cost_jpy(input_tokens, output_tokens, model=None):
    critical_model = str(CONFIG.get("openai_critical_model", "gpt-5.6-terra"))
    if model == critical_model:
        input_rate = float(CONFIG.get("openai_critical_input_usd_per_million", 1.00))
        output_rate = float(CONFIG.get("openai_critical_output_usd_per_million", 6.00))
    else:
        input_rate = float(CONFIG.get("openai_input_usd_per_million", 0.10))
        output_rate = float(CONFIG.get("openai_output_usd_per_million", 0.60))
    input_usd = input_tokens / 1_000_000 * input_rate
    output_usd = output_tokens / 1_000_000 * output_rate
    usd = input_usd + output_usd
    return usd, usd * float(CONFIG.get("budget_usd_to_jpy", 170.0))


def remaining_openai_budget_jpy():
    usage = load_openai_usage()
    if usage.get("blocked"):
        return 0.0
    limit = float(CONFIG.get("monthly_translation_budget_jpy", 1500))
    return max(0.0, limit - float(usage.get("estimated_cost_jpy", 0.0)))


def record_openai_usage(input_tokens, output_tokens, model=None):
    with _OPENAI_USAGE_LOCK:
        usage = load_openai_usage()
        if usage.get("blocked"):
            return usage
        input_tokens = int(input_tokens)
        output_tokens = int(output_tokens)
        usage["input_tokens"] = int(usage.get("input_tokens", 0)) + input_tokens
        usage["output_tokens"] = int(usage.get("output_tokens", 0)) + output_tokens
        usage["requests"] = int(usage.get("requests", 0)) + 1
        usd, jpy = openai_cost_jpy(input_tokens, output_tokens, model=model)
        usage["estimated_cost_usd"] = round(
            float(usage.get("estimated_cost_usd", 0.0)) + usd, 6
        )
        usage["estimated_cost_jpy"] = round(
            float(usage.get("estimated_cost_jpy", 0.0)) + jpy, 2
        )
        per_model = usage.setdefault("per_model", {})
        model_name = str(model or CONFIG.get("openai_model", "gpt-5.6-luna"))
        model_usage = per_model.setdefault(
            model_name, {"input_tokens": 0, "output_tokens": 0, "requests": 0}
        )
        model_usage["input_tokens"] += input_tokens
        model_usage["output_tokens"] += output_tokens
        model_usage["requests"] += 1
        path = openai_usage_path()
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(usage, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
        return usage


def response_output_text(payload):
    direct = str(payload.get("output_text", "")).strip()
    if direct:
        return direct
    pieces = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in ("output_text", "text"):
                value = content.get("text", "")
                if isinstance(value, dict):
                    value = value.get("value", "")
                if value:
                    pieces.append(str(value))
    return "".join(pieces).strip()


_OFFICIAL_REFERENCE_WARNED = set()


def fetch_official_archive_reference(video_id, out_dir=None):
    """財務省などの公式会見概要を取得し、本文だけを参照用に整形する。"""
    if not CONFIG.get("archive_official_reference_enabled", True):
        return ""
    url = str(CONFIG.get("archive_official_reference_urls", {}).get(video_id, "")).strip()
    if not url:
        return ""
    try:
        response = requests.get(
            url,
            timeout=(5, 20),
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        source = _html_to_plain(_decode_official_html(response))
        start = source.find("冒頭発言")
        end = source.find("（以上）", start)
        if start < 0:
            raise RuntimeError("公式ページに冒頭発言がありません")
        if end < 0:
            end = min(len(source), start + 20000)
        reference = source[start:end].strip()
        if len(reference) < 200:
            raise RuntimeError("公式本文が短すぎます")
        if out_dir is not None:
            (Path(out_dir) / "財務省公式会見概要_参照用.txt").write_text(
                f"参照元: {url}\n\n{reference}\n", encoding="utf-8"
            )
        print(f"🏛 財務省公式会見概要: 読み込み済み（{len(reference)}文字）")
        return reference
    except Exception as e:
        print(f"⚠️ 公式会見概要を取得できないため通常認識を続けます: {_official_error_summary(e)}")
        return ""


def correct_with_official_reference(text, reference):
    """アーカイブ認識を公式概要と照合。失敗・不一致時は必ず原文へ戻す。"""
    original = re.sub(r"\s+", " ", str(text)).strip()
    if not reference or len(original) < 40:
        return original
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key or remaining_openai_budget_jpy() < 5.0:
        return original
    instructions = (
        "あなたは日本の官公庁会見の校閲者です。音声認識文を、公式会見概要の一致する箇所だけを根拠に訂正してください。"
        "固有名詞、数字、金融・財政用語、同音異義語、脱落した短い語句を直します。"
        "公式概要は逐語録とは限らないため、認識文にない別の質問や回答を追加せず、要約・意見・説明もしません。"
        "話者名、問）、答）、Markdown、引用符を付けず、訂正後の本文だけを出力してください。"
        "一致箇所を判断できなければ、音声認識文を一字も変えず返してください。"
    )
    payload = {
        "model": str(CONFIG.get("openai_model", "gpt-5.6-luna")),
        "instructions": instructions,
        "input": json.dumps(
            {
                "audio_transcription": original,
                "official_reference": reference[:16000],
            },
            ensure_ascii=False,
        ),
        "max_output_tokens": 1400,
    }
    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=max(15, int(CONFIG.get("openai_request_timeout_seconds", 30))),
        )
        response.raise_for_status()
        data = response.json()
        usage_data = data.get("usage", {})
        record_openai_usage(
            usage_data.get("input_tokens", 0), usage_data.get("output_tokens", 0),
            model=str(CONFIG.get("openai_model", "gpt-5.6-luna")),
        )
        corrected = re.sub(r"\s+", " ", response_output_text(data)).strip()
        corrected = re.sub(r"^```(?:text)?\s*|\s*```$", "", corrected, flags=re.I)
        if (
            not corrected
            or re.search(r"(?:^|\s)(?:問|答)[）)]", corrected)
            or "Markdown" in corrected
        ):
            return original
        ratio = len(corrected) / max(1, len(original))
        if not 0.55 <= ratio <= 1.8:
            return original
        if corrected != original:
            print(f"🏛 公式概要で認識補正: {original[:28]} → {corrected[:28]}")
        return corrected
    except Exception as e:
        key = _translation_error_summary(e)
        if key not in _OFFICIAL_REFERENCE_WARNED:
            _OFFICIAL_REFERENCE_WARNED.add(key)
            print(f"⚠️ 公式概要による認識補正を使えないため通常結果を使用: {key}")
        return original



def important_statement_marker(text, speaker=None):
    """金融市場に関わる方針・判断の候補。質問や単なる話題語は除外する。"""
    if not CONFIG.get("important_statement_marking", True):
        return ""
    if speaker and any(x in str(speaker) for x in ("記者", "司会", "質問")):
        return ""
    t = str(text).casefold()
    if re.search(r"(?:ですか|ますか|でしょうか|[？?])\s*$", t):
        return ""
    topics = (
        "為替", "介入", "円安", "円高", "金利", "利率", "利上げ", "利下げ",
        "消費税", "税率", "減税", "防衛費",
        "金融政策", "物価", "インフレ", "国債", "財政", "関税",
        "原油", "石油備蓄", "経済見通し", "景気", "雇用", "賃金",
        "intervention", "exchange rate", "yen", "interest rate",
        "rate cut", "rate hike", "inflation", "monetary policy", "tariff",
        "treasury", "fiscal", "oil", "employment",
    )
    judgments = (
        "実施", "決定", "検討", "必要", "可能性", "排除", "対応", "措置",
        "注視", "懸念", "過度", "急激", "断固", "引き上げ", "引き下げ",
        "据え置", "維持", "見通し", "見込", "予想", "目標", "達成",
        "上昇", "低下", "拡大", "縮小", "加速", "減速", "改善", "悪化",
        "否定", "考えて", "方針", "予定",
        "will", "would", "expect", "decid", "consider", "remain",
        "concern", "excessive", "necessary", "rule out", "not", "no ",
    )
    relevant = any(x in t for x in topics)
    substantive = any(x in t for x in judgments) or bool(
        re.search(r"\d+(?:\.\d+)?\s*(?:%|％|兆|億|円|bp|basis points)", t)
    )
    return "〇 " if relevant and substantive else ""


def is_critical_finance_text(text):
    """誤訳の影響が大きい金融発言を上位モデルへ振り分ける。"""
    t = str(text).casefold()
    terms = (
        "intervention", "exchange rate", "foreign exchange", "yen", "dollar",
        "interest rate", "rate cut", "rate hike", "basis point", "inflation",
        "federal reserve", "fed ", "bank of japan", "monetary policy",
        "treasury yield", "tariff", "fiscal", "deficit", "debt ceiling",
        "not ", "no ", "never", "wouldn't", "won't", "cannot", "can't",
        "介入", "為替", "円安", "円高", "金利", "利上げ", "利下げ", "物価",
        "インフレ", "金融政策", "財政", "赤字", "国債", "関税",
        "ない", "ません", "否定",
    )
    return bool(re.search(r"\d", t)) or any(term in t for term in terms)


def openai_translate_finance(text, context=""):
    """英語原文を金融会見向けの日本語へ翻訳し、実使用量を月次記録する。"""
    if not CONFIG.get("openai_translation_enabled", True):
        return None
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("⚠️ OPENAI_API_KEY未設定のため、無料翻訳へ切り替えます。")
        return None

    # 1リクエスト分の余裕を残して停止する。実請求額そのものではなく、
    # 設定料金とAPI返却トークン数から計算する保守的なアプリ内上限。
    remaining = remaining_openai_budget_jpy()
    if remaining < 5.0:
        limit = float(CONFIG.get("monthly_translation_budget_jpy", 1500))
        print(f"🛑 OpenAI月間予算上限（{limit:.0f}円）に達したため、無料翻訳へ切り替えます。")
        return None

    critical = is_critical_finance_text(text)
    selected_model = str(
        CONFIG.get("openai_critical_model", "gpt-5.6-terra")
        if critical
        else CONFIG.get("openai_model", "gpt-5.6-luna")
    )
    if critical:
        print("🔎 重要な金融発言を検出: 上位モデルTerraで翻訳します")

    instructions = (
        "あなたは中央銀行・財務省・為替市場を専門とする速報翻訳者です。"
        "英語を自然で簡潔な日本語に忠実に翻訳してください。要約や推測はしません。"
        "否定、時制、条件表現、数値、単位、固有名詞を絶対に変えないでください。"
        "intervention=為替介入、Treasury yields=米国債利回り、basis points=ベーシスポイント、"
        "Federal Reserve=FRB、Bank of Japan=日本銀行、Scott Bessent=スコット・ベッセント財務長官。"
        "文体はです・ます調に統一してください。文脈で意味が明確な慣用句・専門用語は自然に訳してください。"
        "音声認識の崩れで意味が不明な箇所は、推測で埋めず（聞き取り不明）と示してください。"
        "参考文脈は代名詞や指示語の解釈だけに使い、参考文脈自体は訳文に再出力しないでください。"
        "明白な言い直しは意味を保って自然につなぎ、異なる主張や強調、数値は省略しないでください。"
        "原文が未完の場合、結論を創作せず、未完と分かる表現にしてください。"
        "訳文だけを返してください。"
    )
    payload = {
        "model": selected_model,
        "instructions": instructions,
        "input": json.dumps({"reference_context": context, "text_to_translate": text}, ensure_ascii=False),
        "max_output_tokens": 800,
    }
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=max(10, int(CONFIG.get("openai_request_timeout_seconds", 30))),
    )
    response.raise_for_status()
    data = response.json()
    translated = response_output_text(data)
    if not translated:
        raise RuntimeError("OpenAIの翻訳結果が空です")
    usage_data = data.get("usage", {})
    usage = record_openai_usage(
        usage_data.get("input_tokens", 0), usage_data.get("output_tokens", 0),
        model=selected_model,
    )
    limit = float(CONFIG.get("monthly_translation_budget_jpy", 1500))
    print(
        f"💰 OpenAI今月概算: {usage['estimated_cost_jpy']:.2f}円 / {limit:.0f}円 "
        f"（残り約{max(0.0, limit - usage['estimated_cost_jpy']):.2f}円）"
    )
    return translated


def _compact_compare(text):
    return re.sub(r"\s+", "", str(text))


def fallback_post_boundary(text, force=False):
    """AIを使えない場合も、未完文をできるだけ次へ持ち越す。"""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return [], ""
    endings = [m.end() for m in re.finditer(r"[。！？!?](?:[」』\"']|$)", text)]
    if endings:
        cut = endings[-1]
        return [text[:cut].strip()], text[cut:].strip()
    if force or len(text) >= int(CONFIG.get("ai_post_max_buffer_chars", 500)):
        return [text], ""
    return [], text


def openai_decide_post_boundary(text, source_language="ja", force=False):
    """意味が完結した部分だけを投稿候補にし、未完部分を次回へ残す。"""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return [], ""
    if not CONFIG.get("ai_post_boundary_enabled", True):
        return fallback_post_boundary(text, force)
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key or remaining_openai_budget_jpy() < 5.0:
        return fallback_post_boundary(text, force)

    prefix = "【AI速報訳・確認中】" if source_language == "en" else "【AI文字起こし・確認中】"
    max_body = max(60, int(CONFIG.get("post_assistant_max_chars", 180)) - len(prefix) - 10)
    instructions = (
        "あなたはYouTube速報投稿の編集者です。入力文を一字も言い換えず、追加・削除・要約せず、"
        "意味が完結して今すぐ投稿できる部分だけreadyへ分けてください。"
        "文、数値、否定表現、引用の途中では切らないでください。"
        "短文一つずつに分けず、同じ話題の説明と指示語・代名詞を含む後続文は文字数上限内でまとめてください。"
        f"readyの各要素は{max_body}文字以内にしてください。"
        "末尾が未完ならremainderへそのまま残してください。"
        "force=trueなら末尾も可能な限り自然な位置でreadyへ入れてください。"
        "JSON以外は出力しません。形式は"
        '{"ready":["投稿可能な原文"],"remainder":"未完の原文"}です。'
    )
    payload = {
        "model": str(CONFIG.get("openai_model", "gpt-5.6-luna")),
        "instructions": instructions,
        "input": json.dumps(
            {"language": source_language, "force": bool(force), "text": text},
            ensure_ascii=False,
        ),
        "max_output_tokens": 1000,
    }
    try:
        for attempt in range(2):
            request_payload = dict(payload)
            if attempt:
                request_payload["instructions"] = (
                    instructions
                    + "前回の応答はJSONとして解析できませんでした。"
                    + "今回は前置き・説明・Markdownを一切付けず、必ず有効なJSONオブジェクトだけを返してください。"
                )
                print("🔄 AI投稿区切り判定のJSON形式を修正して再試行します。")

            response = requests.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=request_payload,
                timeout=max(10, int(CONFIG.get("openai_request_timeout_seconds", 30))),
            )
            response.raise_for_status()
            data = response.json()
            usage_data = data.get("usage", {})
            record_openai_usage(
                usage_data.get("input_tokens", 0), usage_data.get("output_tokens", 0),
                model=str(CONFIG.get("openai_model", "gpt-5.6-luna")),
            )
            raw = response_output_text(data).strip()
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
            # JSONの前後に短い説明が混ざった場合も、オブジェクト部分だけを救済する。
            first_brace = raw.find("{")
            last_brace = raw.rfind("}")
            if first_brace >= 0 and last_brace > first_brace:
                raw = raw[first_brace:last_brace + 1]
            try:
                decision = json.loads(raw)
                if not isinstance(decision, dict):
                    raise ValueError("AI区切り結果がJSONオブジェクトではありません")
                ready = [str(x).strip() for x in decision.get("ready", []) if str(x).strip()]
                remainder = str(decision.get("remainder", "")).strip()

                # AIが原文を書き換えた場合は採用せず、安全な区切りへ戻す。
                if _compact_compare("".join(ready) + remainder) != _compact_compare(text):
                    raise ValueError("AI区切り結果が原文と一致しません")
                if any(len(piece) > max_body for piece in ready):
                    raise ValueError("AI区切り結果が文字数上限を超えました")
                return ready, remainder
            except (json.JSONDecodeError, ValueError):
                if attempt == 0:
                    continue
                raise
    except Exception as e:
        print(f"⚠️ AI投稿区切り判定を使えないため句読点判定へ切替: {_translation_error_summary(e)}")
        return fallback_post_boundary(text, force)


def _translation_error_summary(error):
    """巨大なリクエストURLや認証文字列を画面へ表示しない。"""
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status:
        return f"HTTP {status}"
    return type(error).__name__


def _provider_available(name):
    return time.time() >= _TRANSLATION_COOLDOWN_UNTIL.get(name, 0)


def _cooldown_provider(name, error):
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    minutes = int(CONFIG.get("translation_cooldown_minutes", 30))
    # 429/403は利用制限の可能性が高い。その他の一時障害は短めに休止する。
    wait_seconds = max(60, minutes * 60) if status in (403, 429) else 120
    _TRANSLATION_COOLDOWN_UNTIL[name] = time.time() + wait_seconds
    print(f"ℹ️ {name}翻訳を一時休止し、別の翻訳先へ切替: {_translation_error_summary(error)}")


def _bing_translate(text, timeout):
    """Bing TranslatorのWebセッションを利用する（APIキー不要）。"""
    session = _BING_TRANSLATOR_STATE.get("session")
    token = _BING_TRANSLATOR_STATE.get("token")
    key = _BING_TRANSLATOR_STATE.get("key")
    ig = _BING_TRANSLATOR_STATE.get("ig")
    if not all((session, token, key, ig)):
        session = requests.Session()
        page = session.get(
            "https://www.bing.com/translator",
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        page.raise_for_status()
        token_match = re.search(
            r"params_AbusePreventionHelper\s*=\s*\[\s*(\d+)\s*,\s*\"([^\"]+)\"",
            page.text,
        )
        ig_match = re.search(r'IG:\"([^\"]+)\"', page.text)
        if not token_match or not ig_match:
            raise RuntimeError("Bing session information not found")
        key, token, ig = token_match.group(1), token_match.group(2), ig_match.group(1)
        _BING_TRANSLATOR_STATE.update(
            {"session": session, "token": token, "key": key, "ig": ig}
        )

    response = session.post(
        "https://www.bing.com/ttranslatev3",
        params={"isVertical": "1", "IG": ig, "IID": "translator.5028.1"},
        data={
            "fromLang": "en", "to": "ja", "text": text,
            "token": token, "key": key,
            "tryFetchingGenderDebiasedTranslations": "true",
        },
        timeout=timeout,
        headers={"Referer": "https://www.bing.com/translator"},
    )
    response.raise_for_status()
    payload = response.json()
    translated = payload[0]["translations"][0]["text"].strip()
    if not translated:
        raise RuntimeError("Bing translation result is empty")
    return html.unescape(translated)


def has_obvious_negation_mismatch(original, translated):
    # no longer が「義務が生じる」と訳された、今回確認済みの明白な逆転。
    # 一般的な翻訳精度を保証する検査ではない。
    return bool(re.search(r"\bno longer\b", original, re.I) and
                re.search(r"もはや[^。！？]*なければならなくな", translated))


def translate_english_to_japanese(text, context=""):
    translated = _translate_english_to_japanese_unchecked(text, context=context)
    if translated and has_obvious_negation_mismatch(text, translated):
        print("⚠️ 否定表現の明白な不整合を検出。訳の表示・投稿候補化を保留します。英語原文は保存します。")
        return None
    return translated


def _translate_english_to_japanese_unchecked(text, context=""):
    """OpenAI金融翻訳を優先し、障害・予算到達時は無料翻訳へ切替する。"""
    if not CONFIG.get("translate_english_to_japanese", True):
        return None
    timeout = max(5, int(CONFIG.get("translation_timeout_seconds", 20)))

    try:
        translated = openai_translate_finance(text, context=context)
        if translated:
            return translated
    except Exception as openai_error:
        print(
            "⚠️ OpenAI翻訳に失敗したため、無料翻訳へ切り替えます: "
            f"{_translation_error_summary(openai_error)}"
        )

    if _provider_available("Bing"):
        try:
            return _bing_translate(text, timeout)
        except Exception as bing_error:
            _BING_TRANSLATOR_STATE.clear()
            _cooldown_provider("Bing", bing_error)

    # 旧translate.googleapis.comは連続利用で429になりやすいため使わない。
    if _provider_available("Google"):
        try:
            response = requests.get(
                "https://clients5.google.com/translate_a/t",
                params={
                    "client": "dict-chrome-ex", "sl": "en", "tl": "ja", "q": text
                },
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, list):
                translated = "".join(x for x in payload if isinstance(x, str)).strip()
                if translated:
                    return translated
            raise RuntimeError("Google translation result is empty")
        except Exception as google_error:
            _cooldown_provider("Google", google_error)

    # MyMemoryは1回の文字数制限があるため、単語境界で分割する。
    words = text.split()
    pieces = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > 450:
            pieces.append(current)
            current = word
        else:
            current = candidate
    if current:
        pieces.append(current)

    if not _provider_available("MyMemory"):
        return None

    translated_pieces = []
    for piece in pieces:
        try:
            response = requests.get(
                "https://api.mymemory.translated.net/get",
                params={"q": piece, "langpair": "en|ja"},
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
            payload = response.json()
            translated = str(payload.get("responseData", {}).get("translatedText", "")).strip()
            if not translated:
                raise RuntimeError(payload.get("responseDetails", "翻訳結果が空です"))
            translated_pieces.append(translated)
        except Exception as e:
            _cooldown_provider("MyMemory", e)
            return None
    return " ".join(translated_pieces).strip() or None


def split_for_chat(text, prefix):
    """YouTubeチャットへ貼りやすい長さに分割する（投稿はしない）。"""
    max_chars = max(80, int(CONFIG.get("post_assistant_max_chars", 180)))
    body_limit = max_chars - len(prefix) - 10
    pieces = []
    remaining = re.sub(r"\s+", " ", text).strip()
    while remaining:
        if len(remaining) <= body_limit:
            pieces.append(remaining)
            break
        cut = -1
        # まず文末を探す。なければ読点を後ろから調べ、数値・専門語・
        # 接続表現の途中になる位置を避ける。
        for marks in (("。", "！", "？", ".", "!", "?"), ("、", ",", ";", " ")):
            candidates = sorted(
                {
                    match.start()
                    for mark in marks
                    for match in re.finditer(re.escape(mark), remaining[:body_limit])
                },
                reverse=True,
            )
            for position in candidates:
                if position < body_limit // 2:
                    continue
                after = remaining[position + 1:].lstrip()
                before = remaining[:position].rstrip()
                if re.match(r"^[0-9０-９.%％]", after):
                    continue
                if before.endswith(("と", "及び", "および", "また", "消費税", "税率", "金利")):
                    continue
                cut = position + 1
                break
            if cut > 0:
                break
        if cut < 0:
            cut = body_limit
            # 英数字・数値・単位の連続部分を真ん中で切らない。
            while cut > body_limit // 2 and re.match(
                r"[A-Za-z0-9０-９.%％]", remaining[cut:cut + 1]
            ):
                cut -= 1
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()

    # 「(4/5) ...こととし、」「(5/5) 関係省庁が...」のように、
    # 最終片だけが短く不自然になる場合は、通常上限を少し超えても結合する。
    if len(pieces) >= 2:
        tail_merge_max = max(
            max_chars,
            int(CONFIG.get("post_assistant_tail_merge_max_chars", 260)),
        )
        combined = f"{pieces[-2]} {pieces[-1]}".strip()
        combined_length = len(prefix) + len(combined) + 10
        short_tail_limit = max(35, body_limit // 2)
        if len(pieces[-1]) <= short_tail_limit and combined_length <= tail_merge_max:
            pieces[-2:] = [combined]
    if len(pieces) <= 1:
        return [f"{prefix}{pieces[0]}"] if pieces else []
    return [f"{prefix}({i}/{len(pieces)}) {piece}" for i, piece in enumerate(pieces, 1)]


def enqueue_post_assistant(text, source_language, speaker=None):
    if not CONFIG.get("post_assistant_enabled", True) or not text.strip():
        return
    prefix = "【AI速報訳・確認中】" if source_language == "en" else "【AI文字起こし・確認中】"
    prefix = important_statement_marker(text, speaker) + prefix
    if speaker:
        prefix += f"\n【{speaker}】"
    for message in split_for_chat(text, prefix):
        POST_ASSIST_QUEUE.put(message)
        with _POST_ASSIST_FILE_LOCK:
            path = DATA_DIR / f"投稿用テキスト_{datetime.now():%Y-%m-%d}.txt"
            with path.open("a", encoding="utf-8") as f:
                f.write(message + "\n")


def start_post_assistant_window(pause_event=None):
    """候補をため、選択した1件をボタンでクリップボードへコピーする。"""
    if not CONFIG.get("post_assistant_enabled", True):
        return

    window_closed = threading.Event()

    def window_main():
        try:
            import tkinter as tk
            from tkinter import messagebox
        except Exception as e:
            print(f"⚠️ 投稿補助ウィンドウを開けません: {e}")
            window_closed.set()
            return

        root = tk.Tk()
        root.title("YouTube投稿補助 - 自動投稿はしません")
        root.geometry("760x520")
        root.attributes("-topmost", bool(CONFIG.get("post_assistant_always_on_top", True)))
        items = []
        copied = set()

        status = tk.StringVar(value="投稿候補を待っています…")
        tk.Label(root, textvariable=status, anchor="w").pack(fill="x", padx=10, pady=(10, 4))
        list_frame = tk.Frame(root)
        list_frame.pack(fill="both", expand=False, padx=10)
        list_scrollbar = tk.Scrollbar(list_frame, orient="vertical")
        listbox = tk.Listbox(
            list_frame, height=11, font=("Yu Gothic UI", 10),
            yscrollcommand=list_scrollbar.set,
        )
        list_scrollbar.config(command=listbox.yview)
        list_scrollbar.pack(side="right", fill="y")
        listbox.pack(side="left", fill="both", expand=True)
        detail = tk.Text(root, height=10, wrap="word", font=("Yu Gothic UI", 12))
        detail.pack(fill="both", expand=True, padx=10, pady=8)

        def show_selected(_event=None):
            selected = listbox.curselection()
            if not selected:
                return
            text = items[selected[0]]
            detail.delete("1.0", "end")
            detail.insert("1.0", text)

        def copy_selected(_event=None):
            selected = listbox.curselection()
            if not selected:
                messagebox.showinfo("投稿補助", "コピーする候補を選んでください。")
                return
            index = selected[0]
            text = items[index]
            root.clipboard_clear()
            root.clipboard_append(text)
            root.update()
            copied.add(index)
            listbox.delete(index)
            listbox.insert(index, "✅ コピー済み: " + text[:55])
            status.set("コピーしました。YouTubeチャットで Ctrl+V を押してください。")

        def copy_next():
            for index in range(len(items)):
                if index not in copied:
                    listbox.selection_clear(0, "end")
                    listbox.selection_set(index)
                    listbox.see(index)
                    show_selected()
                    copy_selected()
                    return
            status.set("未コピーの候補はありません。")

        def toggle_pause():
            if pause_event is None:
                return
            if pause_event.is_set():
                pause_event.clear()
                pause_button.config(text="一時停止")
                status.set("文字起こしを再開しました。")
            else:
                pause_event.set()
                pause_button.config(text="再開")
                status.set("一時停止中です。もう一度押すと続きから再開します。")

        buttons = tk.Frame(root)
        buttons.pack(fill="x", padx=10, pady=(0, 10))
        tk.Button(buttons, text="選択した文章をコピー", command=copy_selected, width=24).pack(side="left")
        tk.Button(buttons, text="次の未コピーをコピー", command=copy_next, width=24).pack(side="left", padx=8)
        if pause_event is not None:
            pause_button = tk.Button(buttons, text="一時停止", command=toggle_pause, width=12)
            pause_button.pack(side="left", padx=8)
        listbox.bind("<<ListboxSelect>>", show_selected)
        root.bind("<Control-c>", copy_selected)
        root.bind("<Return>", copy_selected)

        def poll_queue():
            added = 0
            while True:
                try:
                    message = POST_ASSIST_QUEUE.get_nowait()
                except queue.Empty:
                    break
                items.append(message)
                listbox.insert("end", f"未コピー: {message[:60]}")
                added += 1
            if added:
                pending = len(items) - len(copied)
                status.set(f"未コピー {pending}件。候補は流れず、この画面に残ります。")
                if not listbox.curselection():
                    listbox.selection_set("end")
                    show_selected()
                # 新しい候補が追加されたら、常に最新候補が見える位置へ移動する。
                listbox.see("end")
            while True:
                try:
                    status.set(POST_ASSIST_STATUS_QUEUE.get_nowait())
                except queue.Empty:
                    break
            root.after(300, poll_queue)

        def close_window():
            window_closed.set()
            root.destroy()

        root.after(300, poll_queue)
        root.protocol("WM_DELETE_WINDOW", close_window)
        try:
            root.mainloop()
        finally:
            window_closed.set()

    threading.Thread(target=window_main, daemon=True).start()
    return window_closed


def transcribe_with_retry(model, wav, video_id=None, source_language="ja"):
    """通常認識。異常反復ならプロンプト/前文依存を切って再認識する。"""
    prompt = None if source_language == "en" else WHISPER_PROMPT
    common = dict(
        language=source_language,
        vad_filter=True,
        beam_size=5 if source_language == "en" else max(1, int(CONFIG.get("beam_size", 8))),
        temperature=0.0,
    )
    segments, _ = model.transcribe(
        str(wav),
        condition_on_previous_text=False,
        initial_prompt=prompt,
        **common
    )
    first = list(segments)
    joined = normalize_text(" ".join(x.text for x in first), video_id)
    if not looks_hallucinated(joined):
        return first, False

    print(f"🔁 異常反復を検出。再認識します: {wav.name}")
    # 再試行は強い語彙誘導を外し、beamも控えめにする。
    segments2, _ = model.transcribe(
        str(wav),
        language=source_language,
        vad_filter=True,
        beam_size=5,
        temperature=0.2,
        condition_on_previous_text=False,
        initial_prompt=None,
    )
    second = list(segments2)
    joined2 = normalize_text(" ".join(x.text for x in second), video_id)
    if looks_hallucinated(joined2):
        print(f"⚠️ 再認識後も異常反復。誤訳・投稿を防ぐため、この音声区間の出力を保留します: {wav.name}")
        return [], True
    return second, True


def is_sentence_end(text):
    t = text.rstrip()
    # 省略符号や敬称のピリオドは文の完結ではない。
    if re.search(r"(?:\.{2,}|…|—|–)[\"’”\']*$", t):
        return False
    if re.search(r"\b(?:Mr|Mrs|Ms|Dr|Jr|Sr|Prof|St|and|but|or|because|that|which|to|of|with|for)\.$", t, re.I):
        return False
    return bool(re.search(r"[。！？.!?][\"’”\']*$", t))


def completed_english_segment_count(items):
    """15秒以上のまとまりを確定。45秒を超えても未完の末尾は残す。"""
    if not items:
        return 0
    duration = items[-1][2] - items[0][1]
    if duration < 15:
        return 0
    if is_sentence_end(items[-1][0]):
        return len(items)
    if duration >= 45:
        for index in range(len(items) - 2, -1, -1):
            if is_sentence_end(items[index][0]):
                return index + 1
    return 0


def translation_batch_ready(text, duration, language, max_merge):
    if language != "en":
        return is_sentence_end(text) or duration >= max_merge
    # 英語は短文をまとめ、未完の文は次チャンクへ持ち越す。
    # 追加待ち時間を制限するため、最大45秒で一旦確定する。
    return (duration >= 15 and is_sentence_end(text)) or duration >= max(45, max_merge)



def speaker_ranges_for(video_id):
    ranges = CONFIG.get("archive_speaker_ranges", {}).get(video_id, [])
    result = []
    for r in ranges:
        try:
            result.append((parse_hms(r["start"]), parse_hms(r["end"]), r["speaker"]))
        except Exception:
            continue
    return result


def speaker_from_manual_ranges(start, end, ranges):
    midpoint = (start + end) / 2.0
    for a, b, speaker in ranges:
        if a <= midpoint <= b:
            return speaker
    return None


def split_speaker_turn_text(text):
    """40秒認識内に混在した司会・記者・要人の発言を定型句で分ける。"""
    t = re.sub(r"\s+", " ", str(text)).strip()
    if not t:
        return []
    # 文末直後に現れる、会見で信頼度の高い話者交代の合図だけを使う。
    # 通常の「はい」すべてでは分けず、本文中の誤分割を避ける。
    pattern = (
        r"(?<=[。！？?])\s*(?="
        r"幹事社|"
        r"はい[、,]?\s*(?:まず冒頭|その報道|ありがとうございます|原則は)|"
        r"はい[、,]?\s*ありがとうございました|"
        r"申し訳ありません"
        r")"
    )
    return [part.strip() for part in re.split(pattern, t) if part.strip()]


def infer_speaker(text, previous_speaker, official_speaker):
    """軽量な話者推定。手動時間範囲がある場合はそちらを優先する。"""
    t = text.strip()
    previous = str(previous_speaker or "")
    # 会見での所属・氏名の自己紹介は、音声認識が質問末尾を崩しても
    # 最も信頼できる記者開始の合図になる。
    reporter_intro = bool(re.search(
        r"(?:NHK|新聞|通信|ニュース|テレビ|放送|Bloomberg|Reuters|共同|時事|読売|朝日|毎日|産経|日経|TBS|ANN|FNN)"
        r".{0,24}(?:と申します|です)",
        t,
        re.I,
    ))
    if reporter_intro:
        return "記者"

    # 質問が終わった直後の「はい」や回答定型句を、質問者の続きにしない。
    answer_start = bool(re.match(
        r"^(?:はい(?:[、。]|\s)|ありがとうございます|今般の|今日の大綱|"
        r"その報道|その件|これはどちらか|いずれにしても|原則は)",
        t,
    ))
    if "記者" in previous and answer_start:
        return official_speaker

    reporter_cues = (
        "伺います", "お伺い", "お聞きします", "お聞かせください", "教えてください", "でしょうか",
        "ですか", "お願いします", "冒頭少し重複", "確認ですが", "お尋ね",
        "と申します", "どのようにお考え", "見解を", "改めて伺",
        "幹事社", "私から質問", "私の質問", "それでは大臣",
        "ありがとうございました。それでは"
    )
    official_cues = (
        "申し上げ", "考えております", "認識しております", "対応してまいります",
        "取り組んでまいります", "政府として", "内閣として", "お答え",
        "と考えています", "でございます", "決定会合ですが", "はい。まず"
    )
    if any(x in t for x in reporter_cues):
        return "記者"
    if any(x in t for x in official_cues):
        return official_speaker
    # 記者の質問は複数チャンクにまたがるため、明確な回答開始まで維持する。
    if "記者" in previous:
        return previous_speaker
    return previous_speaker or official_speaker


def choose_speaker(start, end, text, previous_speaker, official_speaker, manual_ranges):
    if not CONFIG.get("speaker_labeling", True):
        return ""
    manual = speaker_from_manual_ranges(start, end, manual_ranges)
    if manual:
        return manual
    return infer_speaker(text, previous_speaker, official_speaker)


def start_stream_to_chunks(url, out_dir, stop_event, from_start=True):
    chunks = out_dir / "chunks"
    chunks.mkdir(parents=True, exist_ok=True)
    ffmpeg_exe = find_ffmpeg()
    yt_cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-progress", "--no-playlist",
        "--ffmpeg-location", str(ffmpeg_exe),
        "-f", "bestaudio/best", "-o", "-",
    ]
    if from_start and CONFIG.get("download_from_start", True):
        yt_cmd.append("--live-from-start")
    browser = CONFIG.get("cookies_from_browser", "").strip()
    if browser:
        yt_cmd += ["--cookies-from-browser", browser]
    yt_cmd.append(url)
    seconds = max(10, int(CONFIG.get("chunk_seconds", 20)))
    ff_cmd = [
        ffmpeg_exe, "-hide_banner", "-loglevel", "warning",
        "-i", "pipe:0", "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", "-f", "segment",
        "-segment_time", str(seconds), "-reset_timestamps", "1",
        str(chunks / "%06d.wav"),
    ]
    # stderrをファイルへ流し、長時間LIVEでのパイプ詰まりを防ぐ。
    yt_log_path = out_dir / "音声取得エラー.log"
    ff_log_path = out_dir / "FFmpegエラー.log"
    yt = ff = None
    print("🎙 YouTube音声ストリームを開始...")
    try:
        with yt_log_path.open("wb") as yt_log, ff_log_path.open("wb") as ff_log:
            yt = subprocess.Popen(yt_cmd, stdout=subprocess.PIPE, stderr=yt_log)
            ff = subprocess.Popen(
                ff_cmd, stdin=yt.stdout, stdout=subprocess.DEVNULL, stderr=ff_log
            )
            yt.stdout.close()
            while ff.poll() is None:
                if stop_event.is_set():
                    break
                time.sleep(1)
            if stop_event.is_set():
                for process in (ff, yt):
                    if process.poll() is None:
                        process.terminate()
            rc_ff = ff.wait(timeout=20)
            try:
                rc_yt = yt.wait(timeout=20)
            except subprocess.TimeoutExpired:
                yt.kill()
                rc_yt = yt.wait(timeout=10)
            yt_log.flush()
            ff_log.flush()
        if not stop_event.is_set():
            yt_error = yt_log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            ff_error = ff_log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
            if rc_yt != 0 or rc_ff != 0:
                raise RuntimeError(
                    f"YouTube音声取得コード {rc_yt} / FFmpegコード {rc_ff}\n"
                    f"【音声取得側の詳細】\n{yt_error or '詳細なし'}\n"
                    f"【FFmpeg側の詳細】\n{ff_error or '詳細なし'}"
                )
            if not any(wav.stat().st_size > 44 for wav in chunks.glob("*.wav")):
                raise RuntimeError(f"音声チャンクを取得できませんでした。\n{yt_error}")
    finally:
        for process in (ff, yt):
            if process and process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=10)
                except Exception:
                    pass
    return chunks


def transcribe_chunks(
    chunks_dir, txt_path, stop_event, title, video_id,
    official_speaker=None, source_language="ja", pause_event=None,
    archive_accuracy_mode=False, official_reference="",
    live_reference_video_id=None,
):
    from faster_whisper import WhisperModel

    try:
        import torch
        cuda = torch.cuda.is_available()
    except Exception:
        cuda = False

    device = "cuda" if cuda else "cpu"
    compute = "float16" if cuda else "int8"
    model_name = CONFIG.get("english_transcription_model", "medium") if source_language == "en" else CONFIG["transcription_model"]
    print(f"🧠 Whisperモデルを読み込み中: {model_name} ({device})")
    if source_language == "en":
        print("🌐 英語認識 → 日本語逐次翻訳: ON（英語原文も保存）")
        print("🔗 英語は2チャンクをまとめて認識し、未完の末尾は次回へ持ち越します。")
    print("📝 専門用語ヒント・文つなぎ・誤字補正: ON")
    print("🛡 異常反復検出・自動再認識: ON")
    if archive_accuracy_mode and source_language == "ja":
        print("🎯 アーカイブ精度優先: ON（40秒・2チャンク認識）")
    if CONFIG.get("speaker_labeling", True):
        print("👥 話者ラベル: ON（手動時間範囲 + 軽量推定）")

    model = WhisperModel(model_name, device=device, compute_type=compute)

    next_index = 0
    total_offset = 0.0
    pending_text = ""
    pending_start = None
    pending_end = None
    pending_speaker = None
    previous_speaker = None
    translation_context = ""
    english_pending = []
    post_buffer = ""
    post_buffer_speaker = None
    failed_attempts = {}
    max_merge = float(CONFIG.get("sentence_merge_max_seconds", 30))
    manual_ranges = speaker_ranges_for(video_id)
    official_speaker = official_speaker or CONFIG.get("archive_official_speakers", {}).get(video_id) or CONFIG.get("default_official_speaker", "高市総理")

    def submit_post_text(text, language, speaker=None, force=False):
        nonlocal post_buffer, post_buffer_speaker
        text = re.sub(r"\s+", " ", str(text)).strip()

        # 話者が変わるときは、前の話者の残りを混ぜずに確定する。
        if post_buffer and speaker and post_buffer_speaker and speaker != post_buffer_speaker:
            ready, remainder = openai_decide_post_boundary(
                post_buffer, source_language=language, force=True
            )
            for piece in ready:
                enqueue_post_assistant(piece, language, speaker=post_buffer_speaker)
            if remainder:
                enqueue_post_assistant(remainder, language, speaker=post_buffer_speaker)
            post_buffer = ""

        if text:
            post_buffer = normalize_text(f"{post_buffer} {text}")
            post_buffer_speaker = speaker or post_buffer_speaker
        if not post_buffer:
            return
        min_chars = max(1, int(CONFIG.get("ai_post_min_chars", 20)))
        group_min_chars = max(
            min_chars, int(CONFIG.get("ai_post_group_min_chars", 90))
        )
        max_buffer = max(100, int(CONFIG.get("ai_post_max_buffer_chars", 500)))
        is_important = bool(important_statement_marker(post_buffer, post_buffer_speaker))
        important_min_chars = max(
            min_chars, int(CONFIG.get("ai_post_important_min_chars", 80))
        )
        required_chars = important_min_chars if is_important else group_min_chars
        if not force and len(post_buffer) < required_chars:
            return
        ready, remainder = openai_decide_post_boundary(
            post_buffer,
            source_language=language,
            force=force or len(post_buffer) >= max_buffer,
        )
        for piece in ready:
            enqueue_post_assistant(piece, language, speaker=post_buffer_speaker)
            print(f"📋 AIが投稿可能と判断: {piece[:70]}")
        post_buffer = remainder

    def emit(f, start, end, text, speaker):
        nonlocal translation_context
        text = normalize_text(text, video_id)
        if not text:
            return
        if speaker:
            f.write(f"【{speaker}】\n")
            print(f"【{speaker}】")
        if source_language == "en":
            if CONFIG.get("save_english_original", True):
                original_line = f"{important_statement_marker(text, speaker)}[{fmt(start)} - {fmt(end)}] EN: {text}\n"
                f.write(original_line)
                if CONFIG.get("display_english_original", False):
                    print(original_line, end="")
            translated = translate_english_to_japanese(text, context=translation_context)
            translation_context = text[-1600:]
            if translated:
                line = f"{important_statement_marker(translated, speaker)}[{fmt(start)} - {fmt(end)}] 日本語: {translated}\n"
                submit_post_text(translated, "en", speaker=speaker)
            else:
                line = f"[{fmt(start)} - {fmt(end)}] 日本語: （翻訳失敗・英語原文を参照）\n"
        else:
            line = f"{important_statement_marker(text, speaker)}[{fmt(start)} - {fmt(end)}] {text}\n"
            submit_post_text(text, "ja", speaker=speaker)
        f.write(line)
        f.flush()
        print(line, end="")

    with txt_path.open("a", encoding="utf-8") as f:
        f.write("【YouTube会見・OpenAI金融翻訳・投稿補助版】\n")
        f.write(f"タイトル: {title}\n")
        f.write(f"URL: https://www.youtube.com/watch?v={video_id}\n")
        f.write(f"文字起こし開始: {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"モデル: {model_name} / プログラム: {APP_VERSION}\n")
        f.write(f"音声言語: {'英語（日本語へ逐次翻訳）' if source_language == 'en' else '日本語'}\n")
        recognition_chunks = 2 if source_language == "en" or archive_accuracy_mode else 1
        f.write(f"チャンク: {CONFIG['chunk_seconds']}秒 / 認識単位: {recognition_chunks}チャンク\n")
        f.write("専門用語ヒント: ON / 文つなぎ: ON / 誤字補正: ON / 異常反復の自動再認識: ON\n")
        f.write("〇 = 重要な金融発言の候補（自動判定。誤判定・見逃しあり）\n")
        f.write("話者ラベルは自動推定を含むため、最終確認を推奨します。\n\n")
        f.flush()

        while True:
            if pause_event is not None and pause_event.is_set():
                time.sleep(0.2)
                continue
            wav = chunks_dir / f"{next_index:06d}.wav"
            following_wav = chunks_dir / f"{next_index + 1:06d}.wav"

            # FFmpegが次のチャンクを作成した時点で、現在のWAVは閉じられている。
            # 配信終了後の最終チャンクだけは、ファイルサイズの安定を確認する。
            wav_ready = wav.exists() and following_wav.exists()
            if wav.exists() and stop_event.is_set() and not wav_ready:
                try:
                    size_before = wav.stat().st_size
                    time.sleep(0.5)
                    wav_ready = size_before > 44 and wav.stat().st_size == size_before
                except OSError:
                    wav_ready = False

            read_count = 1
            recognition_wav = wav
            pair_for_accuracy = archive_accuracy_mode and source_language == "ja"
            if (source_language == "en" or pair_for_accuracy) and wav_ready:
                after_pair = chunks_dir / f"{next_index + 2:06d}.wav"
                if following_wav.exists() and (after_pair.exists() or stop_event.is_set()):
                    read_count = 2
                    recognition_wav = chunks_dir.parent / (
                        "英語認識用_2チャンク.wav"
                        if source_language == "en"
                        else "日本語精度優先用_2チャンク.wav"
                    )
                elif not stop_event.is_set():
                    wav_ready = False

            if wav_ready:
                try:
                    if read_count == 2:
                        concatenate_comparison_audio([wav, following_wav], recognition_wav)
                    segments, retried = transcribe_with_retry(
                        model, recognition_wav, video_id, source_language=source_language
                    )

                    if retried and not segments:
                        end_offset = total_offset + read_count * float(CONFIG["chunk_seconds"])
                        f.write(f"[{fmt(total_offset)} - {fmt(end_offset)}] 【認識保留】再認識後も異常反復のため出力を保留。元音声: {wav.name}から{read_count}チャンク\n")
                        f.flush()
                        # 保留区間の前後を一つの発言として結合しない。
                        if english_pending:
                            emit(f, english_pending[0][1], english_pending[-1][2],
                                 " ".join(item[0] for item in english_pending), pending_speaker)
                            english_pending = []
                        translation_context = ""

                    recognized_pieces = []
                    for s in segments:
                        raw_segment = normalize_text(s.text, video_id)
                        current_reference = official_reference
                        if live_reference_video_id:
                            with LIVE_OFFICIAL_REFERENCES_LOCK:
                                current_reference = str(
                                    LIVE_OFFICIAL_REFERENCES.get(
                                        live_reference_video_id, {}
                                    ).get("text", "")
                                ) or current_reference
                        if source_language == "ja" and current_reference:
                            raw_segment = correct_with_official_reference(
                                raw_segment, current_reference
                            )
                            raw_segment = normalize_text(raw_segment, video_id)
                        if not raw_segment:
                            continue
                        segment_start = total_offset + s.start
                        segment_end = total_offset + s.end
                        parts = split_speaker_turn_text(raw_segment)
                        total_chars = max(1, sum(len(part) for part in parts))
                        cursor = segment_start
                        for index, part in enumerate(parts):
                            if index == len(parts) - 1:
                                piece_end = segment_end
                            else:
                                fraction = len(part) / total_chars
                                piece_end = min(segment_end, cursor + (segment_end - segment_start) * fraction)
                            recognized_pieces.append((part, cursor, piece_end))
                            cursor = piece_end

                    for raw, start, end in recognized_pieces:
                        candidate_speaker = choose_speaker(
                            start, end, raw, previous_speaker, official_speaker, manual_ranges
                        )

                        if source_language == "en":
                            if english_pending and candidate_speaker != pending_speaker:
                                emit(f, english_pending[0][1], english_pending[-1][2],
                                     " ".join(item[0] for item in english_pending), pending_speaker)
                                english_pending = []
                            pending_speaker = candidate_speaker
                            english_pending.append((raw, start, end))
                            previous_speaker = candidate_speaker
                            count = completed_english_segment_count(english_pending)
                            if count:
                                ready_items = english_pending[:count]
                                emit(f, ready_items[0][1], ready_items[-1][2],
                                     " ".join(item[0] for item in ready_items), pending_speaker)
                                english_pending = english_pending[count:]
                            continue

                        if not pending_text:
                            pending_text = raw
                            pending_start = start
                            pending_end = end
                            pending_speaker = candidate_speaker
                        else:
                            # 話者が変わったら文が途中でも一旦確定。
                            if candidate_speaker != pending_speaker:
                                emit(f, pending_start, pending_end, pending_text, pending_speaker)
                                pending_text = raw
                                pending_start = start
                                pending_end = end
                                pending_speaker = candidate_speaker
                            else:
                                pending_text = normalize_text(pending_text + " " + raw, video_id)
                                pending_end = end

                        previous_speaker = candidate_speaker

                        duration = (pending_end - pending_start) if pending_start is not None else 0
                        if translation_batch_ready(pending_text, duration, source_language, max_merge):
                            emit(f, pending_start, pending_end, pending_text, pending_speaker)
                            pending_text = ""
                            pending_start = pending_end = None
                            pending_speaker = None

                    total_offset += read_count * float(CONFIG["chunk_seconds"])
                    failed_attempts.pop(next_index, None)
                    next_index += read_count
                    continue
                except Exception as e:
                    failed_attempts[next_index] = failed_attempts.get(next_index, 0) + 1
                    # 配信終了後も壊れた最終ファイルで永久待機しない。
                    if stop_event.is_set() and failed_attempts[next_index] >= 3:
                        print(f"⚠️ 読み取れないチャンクをスキップ: {wav.name} ({e})")
                        total_offset += float(CONFIG["chunk_seconds"])
                        next_index += 1
                        continue
                    print(f"⏳ WAV完成待ち {wav.name}: {e}")
                    time.sleep(2)
                    continue

            if stop_event.is_set():
                newer = sorted(chunks_dir.glob("*.wav"))
                if newer and any(int(p.stem) >= next_index for p in newer):
                    time.sleep(1)
                    continue
                break

            time.sleep(1)

        if english_pending:
            # 配信終了時のみ、残った未完文も原文とともに確定する。
            emit(f, english_pending[0][1], english_pending[-1][2],
                 " ".join(item[0] for item in english_pending), pending_speaker)

        if pending_text:
            emit(f, pending_start or 0, pending_end or pending_start or 0, pending_text, pending_speaker)

        # 配信終了時は、AIに残りを自然な位置で確定させる。
        submit_post_text("", source_language, speaker=post_buffer_speaker, force=True)

        f.write("\n【文字起こし終了】\n")
        f.write(f"終了: {datetime.now().isoformat(timespec='seconds')}\n")
        f.flush()


def archive_media_info(url):
    """yt-dlpから実際の動画タイトルと言語情報を取得する。"""
    command = [
        sys.executable, "-m", "yt_dlp",
        "--skip-download", "--no-warnings",
        "--print", "%(title)s",
        "--print", "%(language)s",
        url,
    ]
    try:
        child_env = os.environ.copy()
        # Windowsでもyt-dlpの日本語メタデータをUTF-8で受け取る。
        child_env["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            command,
            capture_output=True,
            text=False,
            env=child_env,
            timeout=30,
            check=True,
        )
        raw_output = completed.stdout
        try:
            decoded_output = raw_output.decode("utf-8")
        except UnicodeDecodeError:
            # 旧環境でWindows日本語コードとして出力された場合の互換処理。
            decoded_output = raw_output.decode("cp932", errors="replace")
        values = [line.strip() for line in decoded_output.splitlines()]
        actual_title = values[0] if values else ""
        language = values[1].lower() if len(values) >= 2 else ""
        if language in {"na", "none", "null", "unknown"}:
            language = ""
        return actual_title, language
    except Exception as e:
        print(f"⚠️ 動画情報を取得できないためタイトルから判定します: {e}")
        return "", ""


def archive_language_and_speaker(url, supplied_title, video_id):
    """任意のアーカイブ動画について、実タイトルから言語と話者を安全に決める。"""
    known_speaker = CONFIG.get("archive_official_speakers", {}).get(video_id)
    if known_speaker:
        return "ja", known_speaker, supplied_title

    actual_title, metadata_language = archive_media_info(url)
    combined_title = f"{actual_title} {supplied_title}".strip()
    english_terms = (
        "bessent", "treasury", "federal reserve", "powell", "fomc",
        "stanford", "commencement", "english", "u.s.", "united states",
    )
    japanese_chars = len(re.findall(r"[ぁ-んァ-ヶ一-龯]", actual_title))
    ascii_letters = len(re.findall(r"[A-Za-z]", actual_title))
    is_english = (
        metadata_language.startswith("en")
        or any(term in combined_title.lower() for term in english_terms)
        or (ascii_letters >= 8 and japanese_chars == 0)
    )
    language = "en" if is_english else "ja"

    lowered = combined_title.lower()
    if "bessent" in lowered or "ベッセント" in combined_title:
        speaker = "スコット・ベッセント 米財務長官"
    elif "powell" in lowered or "パウエル" in combined_title:
        speaker = "ジェローム・パウエル FRB議長"
    else:
        # 任意のテスト動画を日本の監視対象者だと誤表示しない。
        speaker = "話者未確認"

    shown_title = actual_title or supplied_title
    print(f"🌐 アーカイブ言語判定: {'英語' if language == 'en' else '日本語'}")
    print(f"👤 話者表示: {speaker}")
    if actual_title:
        print(f"🎬 動画タイトル: {actual_title}")
    return language, speaker, shown_title


def process_url(url, title="YouTube Archive"):
    m = re.search(r"(?:v=|youtu\.be/|/live/)([A-Za-z0-9_-]{6,})", url)
    video_id = m.group(1) if m else "archive_test"

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = DATA_DIR / f"{stamp}_{safe_name(title)}_{video_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir = out_dir / "chunks"
    txt_path = out_dir / "会見_アーカイブテスト文字起こし_改善版.txt"

    print("\n" + "=" * 65)
    print("ARCHIVE TRANSCRIPTION TEST - IMPROVED V3")
    print(f"URL: {url}")
    print("Full transcription from the beginning")
    print("=" * 65)

    pause_event = threading.Event()
    window_closed = start_post_assistant_window(pause_event=pause_event)
    stop_event = threading.Event()
    result = {"error": None}

    def stream_worker():
        try:
            start_stream_to_chunks(url, out_dir, stop_event, from_start=False)
        except Exception as e:
            result["error"] = e
            print("Audio stream error:", e)
        finally:
            stop_event.set()

    threading.Thread(target=stream_worker, daemon=True).start()
    chunks_dir.mkdir(parents=True, exist_ok=True)

    try:
        archive_language, archive_speaker, detected_title = archive_language_and_speaker(
            url, title, video_id
        )
        official_reference = fetch_official_archive_reference(video_id, out_dir=out_dir)
        if result["error"] and not any(chunks_dir.glob("*.wav")):
            print("⏹ 音声取得に失敗したため、Whisperの読み込みを省略します。")
            return
        transcribe_chunks(
            chunks_dir, txt_path, stop_event, detected_title, video_id,
            official_speaker=archive_speaker,
            source_language=archive_language,
            pause_event=pause_event,
            archive_accuracy_mode=True,
            official_reference=official_reference,
        )
        if result["error"]:
            print("Audio stream error:", result["error"])
        else:
            print("\nARCHIVE TRANSCRIPTION TEST COMPLETE")
            print(f"Saved: {txt_path}")
    except Exception as e:
        print("Transcription error:", e)
        raise
    finally:
        if window_closed is not None and not window_closed.is_set():
            pause_event.clear()
            POST_ASSIST_STATUS_QUEUE.put(
                "処理が終了しました。内容を確認し、右上の×で閉じてください。"
            )
            print("投稿補助画面は自動で閉じません。確認後、右上の×で閉じてください。")
            window_closed.wait()


def process_conference(item, active, processed, event_type="live"):
    vid = item.get("id", {}).get("videoId")
    if not vid or vid in active or vid in processed:
        return

    title = item["snippet"].get("title", "")
    desc = item["snippet"].get("description", "")
    channel_title = item["snippet"].get("channelTitle", "（不明）")
    if not is_organization_channel(item):
        print(f"⏭ 個人・未登録チャンネルのため除外: {channel_title} / {title}")
        processed.add(vid)
        return
    if is_excluded_broadcast(title):
        print(f"⏭ 見逃し配信のため除外: {title}")
        processed.add(vid)
        return
    person = person_for(title, desc)
    if not person:
        return

    url = f"https://www.youtube.com/watch?v={vid}"
    source_language = "en" if "ベッセント" in person else "ja"

    # 予約Liveは通知だけ行い、配信開始前の音声取得は始めない。
    if event_type == "upcoming":
        processed.add(vid)
        scheduled_start = scheduled_start_for_video(vid)
        ensure_official_reference_watcher(
            vid, person, title, scheduled_start=scheduled_start
        )
        notify(f"{person}の予約LIVEを検出", f"{title}\n{url}")
        return

    active.add(vid)
    ensure_official_reference_watcher(
        vid, person, title, scheduled_start=datetime.now(timezone.utc)
    )
    stop_event = threading.Event()
    manual_stop_event = threading.Event()
    with ACTIVE_TRANSCRIPTIONS_LOCK:
        ACTIVE_TRANSCRIPTIONS[vid] = {
            "stop": stop_event,
            "manual_stop": manual_stop_event,
            "title": title,
        }

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = DATA_DIR / f"{stamp}_{safe_name(title)}_{vid}"
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir = out_dir / "chunks"
    txt_path = out_dir / "会見_リアルタイム文字起こし_改善版.txt"

    notify(f"{person}の発言LIVEを検出", f"{title}\n{url}")

    print("\n" + "=" * 65)
    print(f"▶ {person}")
    print(f"▶ {title}")
    print(f"▶ {url}")
    print("▶ 改善版リアルタイム全文文字起こしを開始します")
    print("▶ この文字起こしだけを止めるには s を入力して Enter")
    print("=" * 65)

    result = {"error": None}

    def stream_worker():
        try:
            start_stream_to_chunks(url, out_dir, stop_event)
        except Exception as e:
            result["error"] = e
            print("❌ 音声ストリームエラー:", e)
        finally:
            stop_event.set()

    threading.Thread(target=stream_worker, daemon=True).start()

    try:
        chunks_dir.mkdir(parents=True, exist_ok=True)
        transcribe_chunks(
            chunks_dir, txt_path, stop_event, title, vid,
            official_speaker=person, source_language=source_language,
            live_reference_video_id=vid,
        )

        if manual_stop_event.is_set():
            notify("会見の文字起こしを手動停止", f"{title}\n保存先: {txt_path}\nLive監視は継続中です。")
        elif result["error"]:
            notify("会見ストリームエラー", f"{title}\n{result['error']}")
        else:
            notify("会見の全文文字起こし終了", f"{title}\n保存先: {txt_path}")
        processed.add(vid)
    except Exception as e:
        print("❌ 文字起こしエラー:", e)
        notify("文字起こしエラー", f"{title}\n{e}")
    finally:
        with ACTIVE_TRANSCRIPTIONS_LOCK:
            ACTIVE_TRANSCRIPTIONS.pop(vid, None)
        active.discard(vid)
        if not CONFIG.get("keep_audio", True):
            for p in chunks_dir.glob("*.wav"):
                try:
                    p.unlink()
                except Exception:
                    pass


def choose_comparison_audio(data_dir, chunk_seconds):
    """直近の英語テストから、崩れた原文の周辺の保存音声を選ぶ。"""
    candidates = []
    for folder in data_dir.iterdir():
        if not folder.is_dir() or not (folder / "chunks").is_dir():
            continue
        txts = list(folder.glob("*文字起こし*.txt"))
        for txt in txts:
            content = txt.read_text(encoding="utf-8-sig", errors="replace")
            if "EN:" in content:
                candidates.append((txt.stat().st_mtime, folder, content))
                break
    if not candidates:
        raise RuntimeError("英語の保存音声が見つかりません。dataフォルダを残した状態で実行してください。")
    _, folder, content = max(candidates, key=lambda x: x[0])
    wavs = sorted(p for p in (folder / "chunks").glob("*.wav") if p.stem.isdigit())
    if not wavs:
        raise RuntimeError(f"保存音声がありません: {folder / 'chunks'}")
    # 実行ごとの時間のずれを避け、TXT内の発言から対象区間を探す。
    start = None
    for line in content.splitlines():
        if "EN:" not in line:
            continue
        if re.search(r"favoritization|price to price|generous works|understatement", line, re.I):
            match = re.match(r"\[(\d+:\d+:\d+)\s*-", line)
            if match:
                start = parse_hms(match.group(1))
                break
    target = int(start // chunk_seconds) if start is not None else int(wavs[0].stem)
    by_index = {int(p.stem): p for p in wavs}
    if target not in by_index:
        raise RuntimeError("対象区間の音声が見つかりません。keep_audio設定とchunksフォルダを確認してください。")
    selected = [by_index[i] for i in range(max(0, target - 1), target + 2) if i in by_index]
    return folder, selected, content


def concatenate_comparison_audio(paths, destination):
    import wave
    expected = None
    with wave.open(str(destination), "wb") as output:
        for path in paths:
            with wave.open(str(path), "rb") as source:
                params = (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype())
                if expected is None:
                    expected = params
                    output.setnchannels(params[0])
                    output.setsampwidth(params[1])
                    output.setframerate(params[2])
                elif params != expected:
                    raise RuntimeError("保存音声の形式が一致しません。")
                output.writeframes(source.readframes(source.getnframes()))
    with wave.open(str(destination), "rb") as result:
        return result.getnframes() / result.getframerate()


def compare_saved_english_audio():
    """APIを呼ばず、同じ保存音声をsmall/mediumで比較する。設定は変更しない。"""
    import gc
    from faster_whisper import WhisperModel

    folder, selected, original = choose_comparison_audio(
        DATA_DIR, max(10, int(CONFIG.get("chunk_seconds", 20)))
    )
    result_dir = DATA_DIR / ("英語認識比較_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    result_dir.mkdir(parents=True, exist_ok=True)
    sample = result_dir / "比較用音声.wav"
    duration = concatenate_comparison_audio(selected, sample)
    report = result_dir / "英語認識比較結果.txt"
    print(f"比較元: {folder.name}")
    print("対象: " + ", ".join(p.name for p in selected))
    print(f"音声: {duration:.1f}秒 / CPU / int8")
    print("初回のmediumモデルはダウンロードします。結果は日本語訳をせず英語のまま保存します。")
    print("精度は正解原稿なしでは自動判定できません。比較結果をチャットへ添付してください。")
    with report.open("w", encoding="utf-8-sig") as output:
        output.write(f"英語認識比較 / プログラム {APP_VERSION}\n")
        output.write(f"比較元: {folder}\n対象: {', '.join(p.name for p in selected)}\n音声時間: {duration:.2f}秒\n")
        output.write("条件: CPU/int8、英語、beam_size=5、temperature=0、ヒントなし、前文依存なし\n")
        output.write("処理時間はモデル読み込みと認識を別々に記録。認識時間/音声時間が1を超える場合、音声の長さ以上の時間がかかっています。\n")
        output.write("モデルの違いに加え、複数チャンクを結合した効果も含みます。正解原稿がないため精度の自動判定はしません。\n\n")
        chunk_seconds = max(10, int(CONFIG.get("chunk_seconds", 20)))
        lo = int(selected[0].stem) * chunk_seconds
        hi = (int(selected[-1].stem) + 1) * chunk_seconds
        output.write("【以前の原文（参考・正解ではありません）】\n")
        for line in original.splitlines():
            match = re.match(r"\[(\d+:\d+:\d+)\s*-\s*(\d+:\d+:\d+)\]\s*EN:", line)
            if match and parse_hms(match.group(1)) < hi and parse_hms(match.group(2)) > lo:
                output.write(line + "\n")
        output.flush()
        for model_name in ("small", "medium"):
            model = None
            output.write(f"\n【{model_name}・結合音声の再認識】\n")
            output.flush()
            try:
                print(f"モデル読み込み中: {model_name}")
                loading_start = time.perf_counter()
                model = WhisperModel(model_name, device="cpu", compute_type="int8")
                load_seconds = time.perf_counter() - loading_start
                print(f"認識中: {model_name}")
                recognition_start = time.perf_counter()
                segments, _ = model.transcribe(
                    str(sample), language="en", vad_filter=True, beam_size=5,
                    temperature=0.0, condition_on_previous_text=False, initial_prompt=None,
                )
                segments = list(segments)
                seconds = time.perf_counter() - recognition_start
                output.write(f"モデル読み込み: {load_seconds:.2f}秒\n認識時間: {seconds:.2f}秒\n認識時間/音声時間: {seconds / duration:.2f}\n")
                for segment in segments:
                    output.write(f"[{segment.start:.2f} - {segment.end:.2f}] {segment.text.strip()}\n")
                if not segments:
                    output.write("音声認識結果なし\n")
                print(f"{model_name}: 認識完了 {seconds:.1f}秒")
            except Exception as error:
                output.write(f"実行できませんでした: {type(error).__name__}: {error}\n")
                print(f"{model_name}の比較に失敗しました: {error}")
            finally:
                output.flush()
                del model
                gc.collect()
    print(f"\n比較結果: {report}")
    if os.name == "nt":
        try:
            os.startfile(str(result_dir))
        except OSError:
            pass


def main():
    ensure_update_only_bat()
    check_for_updates()

    if "--update-only" in sys.argv:
        print(f"✅ 更新確認が完了しました（現在のバージョン {APP_VERSION}）")
        return

    if "--compare-audio" in sys.argv:
        compare_saved_english_audio()
        return

    if len(sys.argv) >= 2 and sys.argv[1].startswith(("http://", "https://")):
        url = sys.argv[1]
        title = sys.argv[2] if len(sys.argv) >= 3 else "YouTube Archive"
        process_url(url, title=title)
        return

    if not CONFIG.get("youtube_api_key"):
        print("config.json の youtube_api_key に YouTube Data API v3 のAPIキーを入れてください。")
        input("Enterで終了...")
        return

    print("=" * 65)
    print(" YouTube会見LIVE監視 / OpenAI金融翻訳・投稿補助版")
    print("=" * 65)
    print("対象: 高市総理 / 片山財務大臣 / 三村財務官")
    print("日銀: 植田総裁 / 内田・氷見野副総裁 / 審議委員6名")
    if CONFIG.get("enable_bessent_monitoring", False):
        print("米国: スコット・ベッセント財務長官（英語→日本語翻訳）")
    else:
        print("米国: スコット・ベッセント財務長官（Live検索を一時停止中）")
    print("改善: 専門用語ヒント / 誤字補正 / 文つなぎ / 話者ラベル")
    interval_minutes = effective_scan_interval_minutes()
    configured_interval = CONFIG.get("scan_interval_minutes", 20)
    try:
        interval_was_raised = float(configured_interval) < 20
    except (TypeError, ValueError):
        interval_was_raised = True
    if interval_was_raised:
        print(f"検索間隔: 設定値{configured_interval}分 → 安全のため20分に自動調整")
    else:
        print(f"検索間隔: {interval_minutes:g}分")
    print("API検索回数: 配信中72回 + 予約24回（最大96回/日）")
    upcoming_interval_minutes = max(
        60.0, float(CONFIG.get("upcoming_scan_interval_minutes", 60))
    )
    print(f"予約Live検索間隔: {upcoming_interval_minutes:g}分")
    print("通常投稿動画: 監視対象外")
    print("見逃し配信・見逃しライブ: 監視対象外")
    print("文字起こしの手動停止: s を入力して Enter（監視は継続）")
    print("公式資料: 財務省・日銀を会見開始5分前から1分間隔で監視")
    print("イベント日: 24時間・配信中Liveを15分間隔（予約検索は起動時のみ）")
    if CONFIG.get("enable_bessent_monitoring", False):
        print("海外主要チャンネル: 2分間隔の直接監視（検索API漏れ対策）")
    print(f"文字起こしチャンク: {CONFIG['chunk_seconds']}秒")
    if CONFIG.get("openai_translation_enabled", True):
        usage = load_openai_usage()
        limit = float(CONFIG.get("monthly_translation_budget_jpy", 1500))
        print(
            f"OpenAI翻訳予算: 月{limit:.0f}円 / "
            f"今月概算{float(usage.get('estimated_cost_jpy', 0)):.2f}円"
        )
    print("投稿補助: 候補を別ウィンドウに保存（自動投稿はしません）")
    print()

    active = set()
    processed = set()
    notified_upcoming = set()
    next_upcoming_scan = 0.0
    startup_upcoming_scan_done = False
    first_scan = True

    start_post_assistant_window()
    threading.Thread(target=manual_stop_listener, daemon=True).start()
    if CONFIG.get("enable_bessent_monitoring", False):
        threading.Thread(
            target=priority_channel_watcher,
            args=(active, processed),
            daemon=True,
        ).start()

    while True:
        try:
            event_day = is_finance_event_day()
            if first_scan:
                print("🔎 起動時確認: 対象者の配信中Liveを検索しています...")
            items = search_live()
            matching_live_count = 0
            for item in items:
                vid = item.get("id", {}).get("videoId")
                snippet = item.get("snippet", {})
                title = snippet.get("title", "")
                desc = snippet.get("description", "")
                if (
                    is_organization_channel(item)
                    and person_for(title, desc)
                    and not is_excluded_broadcast(title)
                ):
                    matching_live_count += 1
                if vid and vid not in active and vid not in processed:
                    threading.Thread(
                        target=process_conference,
                        args=(item, active, processed, "live"),
                        daemon=True
                    ).start()

            if first_scan:
                if matching_live_count:
                    print(f"✅ 起動時確認完了: 対象の配信中Liveを{matching_live_count}件検出")
                else:
                    print("✅ 起動時確認完了: 現在、対象者の配信中Liveはありません")
                first_scan = False

            # 通常日は予約Liveを1時間ごとに検索。イベント日は配信中Liveを
            # 15分間隔で24時間検索し、API上限維持のため予約検索を省略する。
            now_mono = time.monotonic()
            # イベント日も起動直後だけ予約LIVEを確認し、公式資料の
            # 「開始5分前監視」を登録する。その後はAPI上限のため省略する。
            if (not event_day or not startup_upcoming_scan_done) and now_mono >= next_upcoming_scan:
                upcoming_items = search_upcoming()
                for item in upcoming_items:
                    vid = item.get("id", {}).get("videoId")
                    if vid and vid not in notified_upcoming:
                        process_conference(
                            item, active, notified_upcoming, "upcoming"
                        )
                startup_upcoming_scan_done = True
                next_upcoming_scan = now_mono + upcoming_interval_minutes * 60

            if event_day:
                try:
                    sleep_minutes = max(
                        15.0, float(CONFIG.get("event_live_scan_interval_minutes", 15))
                    )
                except (TypeError, ValueError):
                    sleep_minutes = 15.0
            else:
                sleep_minutes = interval_minutes
            time.sleep(int(sleep_minutes * 60))

        except KeyboardInterrupt:
            print("\n終了しました。")
            break
        except YouTubeQuotaExceeded as e:
            wait_seconds = seconds_until_safe_quota_retry()
            retry_at = datetime.now() + timedelta(seconds=wait_seconds)
            print("⚠️ YouTube検索の1日クォータを使い切りました。")
            print(f"   次回検索予定: {retry_at.strftime('%Y-%m-%d %H:%M:%S')}")
            print("   プログラムを閉じなくても、自動的に監視を再開します。")
            try:
                time.sleep(wait_seconds)
            except KeyboardInterrupt:
                print("\n終了しました。")
                break
        except Exception as e:
            print("⚠️ 監視エラー:", e)
            time.sleep(60)


if __name__ == "__main__":
    main()
