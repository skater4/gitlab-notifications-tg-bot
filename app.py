import os
import time
import json
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, quote

import requests
from dotenv import load_dotenv
from flask import Flask, request

# грузим .env
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("В .env не задан TELEGRAM_BOT_TOKEN")

GITLAB_BASE_URL = os.getenv("GITLAB_BASE_URL", "https://gitlab.com")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

GITLAB_WEBHOOK_SECRET = os.getenv("GITLAB_WEBHOOK_SECRET")
FLASK_PORT = int(os.getenv("PORT", "3000"))

# токен для GitLab API (нужен read_api на одобрения)
GITLAB_API_TOKEN = os.getenv("GITLAB_API_TOKEN")
GITLAB_API_TOKEN_TYPE = os.getenv("GITLAB_API_TOKEN_TYPE", "private").lower()  # "private" | "bearer"

# файл, где храним GitLab ID по chat_id
SUBSCRIPTIONS_FILE = Path(__file__).parent / "subscriptions.json"

# стикеры
STICKER_APPROVED   = "CAACAgIAAxkBAAET_XxpG3JHVUs9jrnFl6xvoTrV-1Ki-QACxXUAAq0c4Ujh0t-06aOJXDYE"
STICKER_MERGE_OK   = "CAACAgIAAxkBAAET_GZpGzi5Yf6w2obp5JQ_Bwhdbs1zTgACGQAD7CAzGfgftAqnaujQNgQ"
STICKER_UNAPPROVAL = "CAACAgIAAxkBAAET_H5pGz2J6GfHPuKogykmDg2K9kDtKwACEwAD7CAzGarT2GEZWCDhNgQ"

# Flask-приложение для вебхука
app = Flask(__name__)


def load_subscriptions() -> dict:
    if SUBSCRIPTIONS_FILE.exists():
        try:
            return json.loads(SUBSCRIPTIONS_FILE.read_text("utf-8"))
        except Exception as e:
            print("load_subscriptions error:", e)
            return {}
    return {}


def save_subscriptions(subs: dict) -> None:
    try:
        SUBSCRIPTIONS_FILE.write_text(
            json.dumps(subs, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        print("save_subscriptions error:", e)


# chat_id (str) -> gitlab_id (int)
subscriptions: dict[str, int] = load_subscriptions()


def send_message(chat_id: int, text: str) -> None:
    try:
        resp = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            print("Telegram error:", resp.text)
        resp.raise_for_status()
    except Exception as e:
        print("send_message error:", e)


def send_sticker(chat_id: int, file_id: str) -> None:
    try:
        resp = requests.post(
            f"{TELEGRAM_API}/sendSticker",
            json={"chat_id": chat_id, "sticker": file_id},
            timeout=10,
        )
        if resp.status_code != 200:
            print("Telegram sticker error:", resp.text)
        resp.raise_for_status()
    except Exception as e:
        print("send_sticker error:", e)


def handle_start(chat_id: int) -> None:
    user_lookup_url = f'{GITLAB_BASE_URL.rstrip("/")}/api/v4/users?username=USERNAME'
    send_message(
        chat_id,
        "Привет! 👋\n\n"
        "Чтобы получать уведомления о <b>аппрувах твоих Merge Request</b> в GitLab:\n\n"
        "1. Открой в браузере:\n"
        f'<a href="{user_lookup_url}">{user_lookup_url}</a>\n'
        "2. В ответе найди поле <code>id</code> — это твой GitLab ID.\n"
        "3. Пришли мне это число одним сообщением, например:\n"
        "   <code>15499688</code>\n\n"
        "Я сохраню этот ID для этого чата и буду использовать его, чтобы слать сюда уведомления об аппрувах твоих MR.",
    )


def handle_gitlab_id(chat_id: int, text: str) -> None:
    raw = text.strip()
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError()
    except ValueError:
        send_message(
            chat_id,
            f"❌ Это не похоже на валидный GitLab ID: <b>{raw}</b>.\n"
            "Нужно просто число из поля <code>id</code>.\n"
            "Например: <code>15499688</code>",
        )
        return

    subscriptions[str(chat_id)] = value
    save_subscriptions(subscriptions)

    send_message(
        chat_id,
        f"✅ Сохранил GitLab ID <b>{value}</b> для этого чата.\n"
        "Теперь при аппрувах твоих MR сюда будут приходить уведомления.",
    )


def handle_update(update: dict) -> None:
    message = update.get("message")
    if not message:
        return

    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()

    if not text:
        return

    if text.startswith("/start"):
        handle_start(chat_id)
    elif text.startswith("/"):
        send_message(chat_id, "Команда не поддерживается. Пришли свой GitLab ID числом 🙂")
    else:
        handle_gitlab_id(chat_id, text)


def get_updates(offset: Optional[int]) -> list[dict]:
    params: dict[str, int] = {"timeout": 30}
    if offset is not None:
        params["offset"] = offset

    resp = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=35)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        print("getUpdates not ok:", data)
        return []
    return data.get("result", [])


# ================== GITLAB WEBHOOK ==================

def _api_base_from_payload(payload: dict) -> str:
    web_url = (payload.get("project") or {}).get("web_url") or ""
    try:
        p = urlparse(web_url)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}/api/v4"
    except Exception:
        pass
    return "https://gitlab.com/api/v4"


def _approvals_via_api(payload: dict) -> Optional[int]:
    """Вернёт approved_count (len(approved_by)) или None при ошибке/отсутствии токена."""
    if not GITLAB_API_TOKEN:
        return None

    attrs = payload.get("object_attributes") or {}
    project_id = attrs.get("target_project_id") or (payload.get("project") or {}).get("id")
    iid = attrs.get("iid")
    if not project_id or not iid:
        return None

    api_base = _api_base_from_payload(payload)
    url = f"{api_base}/projects/{project_id}/merge_requests/{iid}/approvals"

    headers = {}
    if GITLAB_API_TOKEN_TYPE == "bearer":
        headers["Authorization"] = f"Bearer {GITLAB_API_TOKEN}"
    else:
        headers["PRIVATE-TOKEN"] = GITLAB_API_TOKEN

    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            print("approvals API error:", r.status_code, r.text)
            return None
        data = r.json()
        approved_by = data.get("approved_by") or []
        return len(approved_by)
    except Exception as e:
        print("approvals API exception:", e)
        return None


def find_chats_for_author(author_id: int) -> list[int]:
    result: list[int] = []
    for chat_id_str, gitlab_id in subscriptions.items():
        try:
            gitlab_id_int = int(gitlab_id)
        except Exception:
            continue
        if gitlab_id_int == author_id:
            try:
                result.append(int(chat_id_str))
            except Exception:
                continue
    return result


def _escape_html(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _branch_url(project_web_url: str, branch: str) -> Optional[str]:
    if not project_web_url or not branch:
        return None
    return f"{project_web_url.rstrip('/')}/-/tree/{quote(branch, safe='')}"


@app.post("/gitlab/webhook")
def gitlab_webhook():
    # проверяем секрет, если задан
    if GITLAB_WEBHOOK_SECRET:
        token = request.headers.get("X-Gitlab-Token")
        if token != GITLAB_WEBHOOK_SECRET:
            return "forbidden", 403

    payload = request.get_json(silent=True) or {}
    if payload.get("object_kind") != "merge_request":
        return "", 200

    attrs = payload.get("object_attributes") or {}
    action = attrs.get("action")

    # интересуют только approved / unapproval
    if action not in ("approved", "unapproval"):
        return "", 200

    author_id = attrs.get("author_id")
    if not author_id:
        return "", 200
    try:
        author_id_int = int(author_id)
    except Exception:
        return "", 200

    chats = find_chats_for_author(author_id_int)
    if not chats:
        return "", 200

    project_ns_path = (payload.get("project") or {}).get("path_with_namespace", "unknown")
    project_web_url = (payload.get("project") or {}).get("web_url") or ""
    mr_title = _escape_html(attrs.get("title") or "")
    iid = attrs.get("iid") or attrs.get("id") or "?"
    mr_url = attrs.get("url") or attrs.get("web_url") or ""
    source_branch = attrs.get("source_branch") or ""
    target_branch = attrs.get("target_branch") or ""
    actor = _escape_html(
        (payload.get("user") or {}).get("name")
        or (payload.get("user") or {}).get("username")
        or "кто-то"
    )

    # --- счётчики ---
    reviewers = payload.get("reviewers") or []
    total_reviewers = len(reviewers) if isinstance(reviewers, list) else 0

    approved_count = _approvals_via_api(payload)
    if approved_count is None:
        approved_count = (
            sum(1 for r in reviewers if r.get("state") == "approved")
            if isinstance(reviewers, list) else 0
        )

    count_text = f"{approved_count} из {total_reviewers}" if total_reviewers > 0 else str(approved_count)

    # статусная строка
    if action == "approved":
        status_line = f"✅ MR ОДОБРЕН ({count_text})"
    else:  # unapproval
        status_line = f"❌ Аппрув снят ({count_text})"

    # ссылки
    project_link = (
        f'<a href="{project_web_url}">{_escape_html(project_ns_path)}</a>'
        if project_web_url else _escape_html(project_ns_path)
    )
    src_url = _branch_url(project_web_url, source_branch)
    tgt_url = _branch_url(project_web_url, target_branch)

    if src_url and tgt_url:
        branch_line = f'<b>Ветка:</b> <a href="{src_url}">{_escape_html(source_branch)}</a> → <a href="{tgt_url}">{_escape_html(target_branch)}</a>\n'
    else:
        branch_line = f'<b>Ветка:</b> {_escape_html(source_branch)} → {_escape_html(target_branch)}\n'

    mr_line = (
        f'<b>MR:</b> <a href="{mr_url}">!{iid}</a> — {mr_title}\n'
        if mr_url else f'<b>MR:</b> !{iid} — {mr_title}\n'
    )

    text = (
        f"{status_line}\n"
        f"<b>Проект:</b> {project_link}\n"
        f"{mr_line}"
        f"{branch_line}"
        f"<b>Аппрувер:</b> {actor}\n"
    )

    # только для approved — добавляем «Можно мержить!» и шлём соответствующие стикеры
    for chat_id in chats:
        if action == "approved":
            if total_reviewers > 0 and approved_count >= total_reviewers:
                text_to_send = text + "\n<b>Можно мержить!</b>"
                send_message(chat_id, text_to_send)
                send_sticker(chat_id, STICKER_MERGE_OK)
            else:
                send_message(chat_id, text)
                send_sticker(chat_id, STICKER_APPROVED)
        else:
            # unapproval: только сообщение и стикер ревока
            send_message(chat_id, text)
            send_sticker(chat_id, STICKER_UNAPPROVAL)

    return "", 200


# ================== RUNNERS ==================

def telegram_poller() -> None:
    print("Telegram poller started...")
    offset: Optional[int] = None
    while True:
        try:
            updates = get_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1
                handle_update(update)
        except KeyboardInterrupt:
            print("Stopping poller by keyboard interrupt")
            break
        except Exception as e:
            print("Error in poller loop:", e)
            time.sleep(5)


def run_flask() -> None:
    print(f"Flask server starting on 0.0.0.0:{FLASK_PORT} ...")
    app.run(host="0.0.0.0", port=FLASK_PORT)


def main() -> None:
    # поднимаем Flask в отдельном потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # запускаем long polling
    telegram_poller()


if __name__ == "__main__":
    main()
