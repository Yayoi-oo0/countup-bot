#!/usr/bin/env python3
"""
Discord カウントアップ通知

起点日（START_DATE）からの日数を数えて、記念日に Discord へ通知します。
  - 毎月、起点日と同じ日 …「1ヶ月」「2ヶ月」…「1年1ヶ月」…
  - 日数の節目（DAY_MILESTONES、初期値は 100 日）
  - 半年
  - 1年、以降毎年
同じ日に重なるもの（半年・◯周年と毎月の通知）は 1 通にまとめて送ります。

1日1回実行すると、通知日のときだけ Discord のウェブフックに投稿します。
bot のトークンを設定すると、チャンネル名に日数を表示する「カウンターチャンネル」の更新と、
/days コマンドの登録もできます（/days への返事は cloudflare/worker.js が担当します）。

  python3 countup.py                   今日が通知日なら送信（＋カウンターチャンネルを更新）
  python3 countup.py --list            これからの通知予定を表示（送信しない）
  python3 countup.py --date 2026-12-27 --dry-run
                                       指定した日の通知内容を表示（送信しない）
  python3 countup.py --test            通知日でなくてもテスト送信
  python3 countup.py --counter-only    カウンターチャンネルの更新だけする
  python3 countup.py --register        /days コマンドを Discord に登録する
  python3 countup.py --at 09:00        9:00（日本時間）ちょうどまで待ってから実行（1時間以内のとき）

環境変数
  DISCORD_WEBHOOK_URL  通知の送信先（ウェブフックの URL）
  DISCORD_BOT_TOKEN    bot のトークン（カウンターチャンネルと /days の登録に使う。通知だけなら不要）
  COUNTER_CHANNEL_ID   名前に日数を表示するチャンネルの ID（使う場合だけ）
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

# ================= 設定 =================
START_DATE = date(2026, 9, 18)  # 起点日
SINCE_TEXT = ""                 # 「〇〇から今日で…」の〇〇。空なら "9/18"。例: "付き合って"
DAY_MILESTONES = [100]          # 日数の節目。例: [100, 200, 300, 500, 1000]
COUNT_FROM_1 = False            # True にすると起点日を「1日目」と数える（100日の通知が1日早まる）
MENTION = ""                    # 通知に付けるメンション。例: "@everyone"、"<@ユーザーID>"
COUNTER_PREFIX = "📅 "          # カウンターチャンネル名の頭に付ける文字
# ※ /days を使う場合は、cloudflare/worker.js の設定も同じ値にしてください
# ========================================

JST = timezone(timedelta(hours=9), "JST")  # 日本時間（サマータイムがないので固定で +9 時間）
WEEKDAYS = "月火水木金土日"


def today_jst() -> date:
    return datetime.now(JST).date()


def since_text() -> str:
    return SINCE_TEXT or f"{START_DATE.month}/{START_DATE.day}"


def add_months(d: date, months: int) -> date:
    """d の months ヶ月後。その月に同じ日がなければ月末にする（1/31 の1ヶ月後 → 2/28）"""
    years, month_index = divmod(d.month - 1 + months, 12)
    year, month = d.year + years, month_index + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def day_count(d: date) -> int:
    """起点日から何日か（COUNT_FROM_1 なら起点日を1日目として数える）"""
    return (d - START_DATE).days + (1 if COUNT_FROM_1 else 0)


def count_text(d: date) -> str:
    return f"{day_count(d)}日目" if COUNT_FROM_1 else f"{day_count(d)}日"


def month_label(n: int) -> tuple[str, str]:
    """n ヶ月目の (絵文字, 呼び方)"""
    years, months = divmod(n, 12)
    if months == 0:
        return "🎉", f"{years}周年"
    if n == 6:
        return "🌸", "半年"
    return "🌙", (f"{years}年{months}ヶ月" if years else f"{months}ヶ月")


def milestones(d: date) -> list[tuple[str, str, str]]:
    """d の記念日を [(種類, 絵文字, 呼び方), ...] で返す。通知日でなければ空リスト"""
    found = []
    n = (d.year - START_DATE.year) * 12 + (d.month - START_DATE.month)
    if n >= 1 and add_months(START_DATE, n) == d:
        found.append(("month", *month_label(n)))
    if d > START_DATE and day_count(d) in DAY_MILESTONES:
        found.append(("day", "💯" if day_count(d) == 100 else "✨", count_text(d)))
    return found


def notification_text(d: date) -> str | None:
    """d が通知日なら通知文、そうでなければ None"""
    found = milestones(d)
    if not found:
        return None
    labels = " & ".join(f"**{label}**" for _, _, label in found)
    text = f"{found[0][1]} {since_text()}から今日で {labels}！"
    if all(kind != "day" for kind, _, _ in found):
        text += f"（{count_text(d)}）"
    return text


def build_message(d: date) -> str | None:
    """実際に送るメッセージ（通知文 + メンション）。通知日でなければ None"""
    text = notification_text(d)
    if text and MENTION:
        text = f"{MENTION} {text}"
    return text


def upcoming(start: date, count: int) -> list[tuple[date, str]]:
    """start 以降の通知を count 件、[(日付, 通知文), ...] で返す"""
    result = []
    d = start
    limit = start + timedelta(days=366 * 100)
    while len(result) < count and d < limit:
        text = notification_text(d)
        if text:
            result.append((d, text))
        d += timedelta(days=1)
    return result


def status_text(d: date) -> str:
    """短いカウント表示（例: 9/18から12日）"""
    if d < START_DATE:
        return f"{since_text()}まであと{(START_DATE - d).days}日"
    return f"{since_text()}から{count_text(d)}"


def days_report(d: date) -> str:
    """今日のカウントと次の記念日（/days の返事やテスト送信で使う）"""
    if d < START_DATE:
        lines = [f"🗓️ {status_text(d)}"]
    else:
        lines = [notification_text(d) or f"🗓️ {since_text()}から今日で **{count_text(d)}**"]
    for when, _ in upcoming(d + timedelta(days=1), 1):
        labels = " & ".join(label for _, _, label in milestones(when))
        lines.append(
            f"⏭️ 次は {when.month}/{when.day}（{WEEKDAYS[when.weekday()]}）の **{labels}**、"
            f"あと{(when - d).days}日"
        )
    return "\n".join(lines)


def counter_name(d: date) -> str:
    """カウンターチャンネルの名前（例: 📅 9/18から12日）"""
    return COUNTER_PREFIX + status_text(d)


# ---------------------------------------------------------------------------
# Discord とのやりとり
# ---------------------------------------------------------------------------

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/, 1.0)"  # Discord が求める形式（ブラウザ風だと弾かれる）
COMMANDS = [
    {"name": "days", "type": 1, "description": "今日で何日か、次の記念日まであと何日かを表示します"},
]

WEBHOOK_HINTS = {
    401: "ウェブフックの URL が正しくありません。Discord で「ウェブフックURLをコピー」し直して、DISCORD_WEBHOOK_URL に登録し直してください",
    404: "ウェブフックが見つかりません（削除された可能性があります）。作り直して、DISCORD_WEBHOOK_URL に登録し直してください",
}
BOT_HINTS = {
    401: "bot のトークンが正しくありません。Developer Portal の「Bot」→「Reset Token」で取り直して、DISCORD_BOT_TOKEN に登録し直してください",
    403: "bot に権限がありません。bot をサーバーに招待したか（applications.commands にもチェック）、カウンターチャンネルの権限で bot に「チャンネルの管理」を許可したか確認してください",
    404: "見つかりません。チャンネルの ID が正しいか、bot がそのサーバーに入っているか確認してください",
}


class DiscordError(Exception):
    def __init__(self, status: int | None, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}" if status else detail)
        self.status = status


class Problem(Exception):
    """利用者に見せるエラー（メッセージをそのまま表示して、終了コード 1 で終わる）"""


def discord_request(method: str, url: str, *, token: str | None = None, body=None):
    """Discord の API を呼んで、返ってきた JSON を返す（一時的なエラーなら少し待って最大3回）"""
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bot {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
            return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            if (e.code == 429 or e.code >= 500) and attempt < 3:
                time.sleep(_retry_after(e.headers.get("Retry-After")))
                continue
            raise DiscordError(e.code, detail) from None
        except urllib.error.URLError as e:
            if attempt < 3:
                time.sleep(5)
                continue
            raise DiscordError(None, str(e.reason)) from None


def _retry_after(value: str | None) -> float:
    try:
        return min(max(float(value), 1.0), 60.0)
    except (TypeError, ValueError):
        return 5.0


def bot_api(method: str, path: str, token: str, body=None):
    return discord_request(method, API + path, token=token, body=body)


def send_webhook(url: str, content: str) -> None:
    """Discord のウェブフックにメッセージを投稿する"""
    discord_request("POST", url, body={"content": content})


def _loose(name: str) -> str:
    # テキストチャンネルは Discord 側で空白が「-」に変わるので、比べるときは空白と「-」を無視する
    return "".join(name.lower().replace("-", " ").split())


def update_counter(token: str, channel_id: str, d: date) -> str:
    """カウンターチャンネルの名前を d の日数にする（すでに同じなら何もしない）"""
    name = counter_name(d)
    channel = bot_api("GET", f"/channels/{channel_id}", token)
    if _loose(channel.get("name", "")) == _loose(name):
        return f"カウンターチャンネルは更新済みです: {channel.get('name')}"
    bot_api("PATCH", f"/channels/{channel_id}", token, {"name": name})
    return f"カウンターチャンネルの名前を変えました: {name}"


def register_commands(token: str, webhook_url: str) -> list[str]:
    """/days コマンドを、ウェブフックがあるサーバーに登録する"""
    app = bot_api("GET", "/applications/@me", token)
    guild_id = discord_request("GET", webhook_url)["guild_id"]
    # サーバーごとのコマンドとして登録する（全サーバー向けより反映が早い）
    bot_api("PUT", f"/applications/{app['id']}/guilds/{guild_id}/commands", token, COMMANDS)
    lines = ["✅ /days コマンドを登録しました"]
    # bot.py を動かしたことがあると全サーバー向けの /days が残っていて二重に出るので消しておく
    if bot_api("GET", f"/applications/{app['id']}/commands", token):
        bot_api("PUT", f"/applications/{app['id']}/commands", token, [])
        lines.append("以前に登録された /days（全サーバー向け）は、二重に出ないように削除しました")
    endpoint = app.get("interactions_endpoint_url")
    if endpoint:
        lines.append(f"Interactions Endpoint URL: {endpoint}")
        lines.append("Discord で /days と打ってみてください")
    else:
        lines.append(
            "⚠️ Interactions Endpoint URL がまだ設定されていません。Developer Portal の"
            "「General Information」で、Cloudflare Workers の URL を設定してください"
        )
    return lines


def describe(what: str, error: DiscordError, hints: dict[int, str]) -> str:
    message = f"{what}に失敗しました（{error}）"
    hint = hints.get(error.status)
    return f"{message}\n→ {hint}" if hint else message


# ---------------------------------------------------------------------------
# 実行
# ---------------------------------------------------------------------------

def run_notification(d: date, test: bool, dry_run: bool, webhook_url: str) -> None:
    message = build_message(d)
    if message is None:
        if not test:
            print(f"{d} は通知日ではありません（{status_text(d)}）")
            return
        message = "🔔 テスト送信です\n" + days_report(d)

    if dry_run:
        print(message)
        return
    if not webhook_url:
        raise Problem("環境変数 DISCORD_WEBHOOK_URL が設定されていません")
    try:
        send_webhook(webhook_url, message)
    except DiscordError as e:
        raise Problem(describe("通知の送信", e, WEBHOOK_HINTS)) from None
    print(f"送信しました:\n{message}")


def run_counter(token: str, channel_id: str) -> None:
    if not token:
        raise Problem("カウンターチャンネルを使うには、DISCORD_BOT_TOKEN も設定してください")
    if not channel_id.isdigit():
        raise Problem("COUNTER_CHANNEL_ID には、チャンネルの ID（数字）を設定してください")
    try:
        print(update_counter(token, channel_id, today_jst()))
    except DiscordError as e:
        raise Problem(describe("カウンターチャンネルの更新", e, BOT_HINTS)) from None


def run_register(token: str, webhook_url: str) -> None:
    if not token:
        raise Problem("環境変数 DISCORD_BOT_TOKEN が設定されていません")
    if not webhook_url:
        raise Problem("環境変数 DISCORD_WEBHOOK_URL が設定されていません（登録先のサーバーを調べるのに使います）")
    try:
        print("\n".join(register_commands(token, webhook_url)))
    except DiscordError as e:
        raise Problem(describe("/days コマンドの登録", e, BOT_HINTS)) from None


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "true"


def seconds_until(at: str) -> float:
    """日本時間の at（"09:00" など）まであと何秒か。1時間より先なら（もう過ぎているときも）0"""
    hour, minute = (int(x) for x in at.split(":"))
    now = datetime.now(JST)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    seconds = (target - now).total_seconds()
    return seconds if seconds <= 60 * 60 else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description="Discord カウントアップ通知")
    parser.add_argument("--date", help="この日付として実行する（YYYY-MM-DD）")
    parser.add_argument("--list", type=int, nargs="?", const=15, metavar="件数",
                        help="これからの通知予定を表示する（送信しない）")
    parser.add_argument("--dry-run", action="store_true", help="送信せずに内容を表示だけする")
    parser.add_argument("--test", action="store_true", help="通知日でなくてもテスト送信する")
    parser.add_argument("--counter-only", action="store_true", help="カウンターチャンネルの更新だけする")
    parser.add_argument("--register", action="store_true", help="/days コマンドを Discord に登録する")
    parser.add_argument("--at", metavar="HH:MM", help="この時刻（日本時間）ちょうどまで待ってから実行する")
    args = parser.parse_args()

    # GitHub Actions から渡される値（手動実行の入力など）は環境変数で受け取る
    date_str = (args.date or os.environ.get("COUNTUP_DATE") or "").strip()
    test = args.test or _flag("COUNTUP_TEST")
    counter_only = args.counter_only or _flag("COUNTUP_COUNTER_ONLY")
    register = args.register or _flag("COUNTUP_REGISTER")
    at = (args.at or os.environ.get("COUNTUP_AT") or "").strip()
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    counter_id = os.environ.get("COUNTER_CHANNEL_ID", "").strip()
    try:
        d = date.fromisoformat(date_str) if date_str else today_jst()
    except ValueError:
        raise SystemExit(f"日付は YYYY-MM-DD の形で指定してください: {date_str}") from None
    try:
        wait = seconds_until(at) if at and not date_str and not register else 0.0
    except ValueError:
        raise SystemExit(f"時刻は HH:MM の形で指定してください: {at}") from None

    if args.list:
        for when, text in upcoming(d, args.list):
            print(f"{when:%Y-%m-%d}({WEEKDAYS[when.weekday()]})  {text.replace('**', '')}")
        return

    # 定期実行は少し早めに起動するので、指定の時刻ちょうどまで待つ
    # （通知の実行は、通知日でなければ待たずに終わる。起動が遅れて時刻を過ぎていたら、すぐ実行する）
    if wait:
        d = (datetime.now(JST) + timedelta(seconds=wait)).date()
        has_work = bool(counter_id) if counter_only else (test or build_message(d) is not None)
        if has_work:
            print(f"{at} まで {wait / 60:.1f} 分待ちます", flush=True)
            time.sleep(wait)

    problems = []
    tasks = []
    if register:
        tasks.append(lambda: run_register(token, webhook_url))
    else:
        if not counter_only:
            tasks.append(lambda: run_notification(d, test, args.dry_run, webhook_url))
        # カウンターチャンネルはいつも「今日」の日数にする（日付を指定したときや --dry-run では触らない）
        if counter_id and not date_str and not args.dry_run:
            tasks.append(lambda: run_counter(token, counter_id))
        elif counter_only and not counter_id:
            print("カウンターチャンネル（COUNTER_CHANNEL_ID）は設定されていないので、何もしません")

    for task in tasks:
        try:
            task()
        except Problem as e:
            problems.append(str(e))
    if problems:
        raise SystemExit("\n".join(problems))


if __name__ == "__main__":
    main()
