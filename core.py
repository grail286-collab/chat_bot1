"""
Общая логика для ботов ВК и Telegram: загрузка ответов и поиск темы по сообщению.
"""
import json
import re
import difflib
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ANSWERS_FILE = BASE_DIR / "answers.json"

START_WORDS = {"начать", "старт", "start", "привет", "здравствуйте", "hi", "hello"}
MENU_WORDS = {"меню", "темы", "список", "menu"}
HELP_WORDS = {"помощь", "help", "справка", "?"}


def load_answers(path=ANSWERS_FILE):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def normalize(text):
    text = (text or "").lower().replace("ё", "е")
    text = re.sub(r"[^\w\s?]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def topic_files(topic):
    """Список существующих файлов темы (пути из answers.json считаются от папки бота)
    и список отсутствующих — чтобы бот мог предупредить.
    Элемент списка files — строка-путь или {"path": ..., "name": "Имя для человека.pdf"}.
    Возвращает found = [(путь, имя_файла_для_отправки)], missing = [путь]."""
    found, missing = [], []
    for item in topic.get("files", []):
        if isinstance(item, dict):
            rel, name = item["path"], item.get("name")
        else:
            rel, name = item, None
        p = (BASE_DIR / rel).resolve()
        if p.is_file():
            found.append((p, name or p.name))
        else:
            missing.append(p)
    return found, missing


def find_topic(text, data):
    """Возвращает (ответ, тема_или_None). Тема нужна, чтобы отправить её файлы."""
    msg = normalize(text.lstrip("/"))
    if not msg or msg in START_WORDS:
        return data["greeting"], None
    if msg in MENU_WORDS:
        lines = []
        for t in data["topics"]:
            mark = " 📎" if t.get("files") else ""
            lines.append(f"• {t['title']}{mark}")
        return "Доступные темы (📎 — с файлами):\n" + "\n".join(lines), None
    if msg in HELP_WORDS:
        return data["help"], None

    topics = data["topics"]

    # 1. Точное совпадение с названием темы (нажатие кнопки)
    for t in topics:
        if msg == normalize(t["title"]):
            return t["answer"], t

    # 2. Ключевые слова: тема с наибольшим числом совпадений
    best, best_score = None, 0
    words = msg.split()
    for t in topics:
        score = 0
        for kw in t["keywords"]:
            kw_n = normalize(kw)
            if " " in kw_n:
                score += 2 if kw_n in msg else 0
            elif any(w.startswith(kw_n) or (kw_n.startswith(w) and len(w) >= 4) for w in words):
                score += 1
        if score > best_score:
            best, best_score = t, score
    if best:
        return best["answer"], best

    # 3. Нечёткое совпадение (опечатки)
    vocab = {}
    for t in topics:
        for kw in t["keywords"] + [t["title"]]:
            vocab[normalize(kw)] = t
    for w in words:
        close = difflib.get_close_matches(w, vocab.keys(), n=1, cutoff=0.75)
        if close:
            t = vocab[close[0]]
            return t["answer"], t

    return data["fallback"], None
