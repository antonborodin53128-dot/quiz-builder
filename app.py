"""Конструктор квизов.

/                       — все квизы, создание и редактирование
/host/<id>?k=<ключ>     — страница ведущего
/play/<id>              — страница участников
/qr/<id>                — полноэкранный QR для участников (для проектора)
/results/<id>           — таблица лидеров для проектора
"""
import json
import os
import secrets
import socket
import urllib.parse
import urllib.request
import uuid
from threading import Lock

from flask import Flask, abort, jsonify, render_template, request

app = Flask(__name__)
lock = Lock()

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
QUIZ_FILE = os.path.join(DATA_DIR, "quizzes.json")
os.makedirs(DATA_DIR, exist_ok=True)

MIN_OPTIONS, MAX_OPTIONS = 2, 8
LETTERS = "АБВГДЕЖЗ"


# ---------- хранилище квизов ----------

def load_quizzes():
    try:
        with open(QUIZ_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_quizzes(data):
    tmp = QUIZ_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, QUIZ_FILE)


QUIZZES = load_quizzes()
GAMES = {}  # quiz_id -> состояние игры (в памяти)


def clean_quiz(x):
    """Проверяет и нормализует квиз из запроса. Возвращает (quiz, error)."""
    title = str(x.get("title", "")).strip()[:80]
    if not title:
        return None, "Введите название квиза"
    rounds = []
    for i, r in enumerate(x.get("rounds") or [], 1):
        question = str(r.get("question", "")).strip()[:300]
        raw = [str(o).strip()[:120] for o in (r.get("options") or [])]
        correct_raw = r.get("correct")
        # индекс правильного ответа считаем по исходному списку, пустые варианты отбрасываем
        options, correct = [], None
        for idx, o in enumerate(raw[:MAX_OPTIONS]):
            if o:
                if idx == correct_raw:
                    correct = len(options)
                options.append(o)
        if not question:
            return None, f"Раунд {i}: введите вопрос"
        if len(options) < MIN_OPTIONS:
            return None, f"Раунд {i}: нужно минимум {MIN_OPTIONS} варианта ответа"
        if correct is None:
            return None, f"Раунд {i}: отметьте правильный ответ"
        rounds.append({"question": question, "options": options, "correct": correct})
    if not rounds:
        return None, "Добавьте хотя бы один раунд"
    return {"title": title, "rounds": rounds}, None


def public_quiz(qid):
    q = QUIZZES[qid]
    return {"id": qid, "title": q["title"], "rounds": q["rounds"], "host_key": q["host_key"]}


# ---------- состояние игры ----------

def game(qid):
    g = GAMES.get(qid)
    if g is None:
        g = GAMES[qid] = {"r": 0, "open": False, "session": str(uuid.uuid4()), "players": {}, "votes": {}}
    # квиз могли отредактировать — не выходим за границы
    g["r"] = min(g["r"], len(QUIZZES[qid]["rounds"]) - 1)
    return g


def get_quiz_or_404(qid):
    if qid not in QUIZZES:
        abort(404)
    return QUIZZES[qid]


def check_host(qid):
    q = get_quiz_or_404(qid)
    key = request.args.get("k") or request.headers.get("X-Host-Key", "")
    if not secrets.compare_digest(str(key), q["host_key"]):
        abort(403)


def leaderboard(qid):
    q, g = QUIZZES[qid], GAMES[qid]
    scores = {d: 0 for d in g["players"]}
    for r, votes in g["votes"].items():
        if r >= len(q["rounds"]):
            continue
        for dev, c in votes.items():
            if dev in scores and c == q["rounds"][r]["correct"]:
                scores[dev] += 1
    return sorted(
        ({"name": g["players"][d], "score": s} for d, s in scores.items()),
        key=lambda x: (-x["score"], x["name"]),
    )


def lan_ip():
    """Адрес компьютера в локальной сети — нужен, чтобы QR работал с телефона при запуске на ПК.

    VPN-адаптеры часто перехватывают маршрут по умолчанию, поэтому собираем все адреса
    и предпочитаем домашние сети 192.168.x.x, затем 10.x.x.x, затем 172.16–31.x.x.
    Можно задать вручную переменной окружения LAN_IP.
    """
    forced = os.environ.get("LAN_IP")
    if forced:
        return forced
    found = set()
    try:
        found.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        found.add(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()

    def rank(ip):
        if ip.startswith("192.168."):
            return 0
        if ip.startswith("10."):
            return 1
        if ip.startswith("172."):
            return 2
        return 3

    usable = [ip for ip in found if not ip.startswith(("127.", "169.254."))]
    return min(usable, key=lambda ip: (rank(ip), ip)) if usable else "127.0.0.1"


@app.get("/api/info")
def info():
    return jsonify(lan_ip=lan_ip())


# ---------- страницы ----------

@app.get("/")
def home():
    return render_template("index.html")


@app.get("/host/<qid>")
def host_page(qid):
    check_host(qid)
    return render_template("host.html", qid=qid, title=QUIZZES[qid]["title"])


@app.get("/play/<qid>")
def play_page(qid):
    q = get_quiz_or_404(qid)
    return render_template("play.html", qid=qid, title=q["title"])


@app.get("/qr/<qid>")
def qr_page(qid):
    q = get_quiz_or_404(qid)
    return render_template("qr.html", qid=qid, title=q["title"])


@app.get("/results/<qid>")
def results_page(qid):
    q = get_quiz_or_404(qid)
    return render_template("results.html", qid=qid, title=q["title"])


# ---------- API: конструктор ----------

@app.get("/api/quizzes")
def quizzes_list():
    return jsonify(quizzes=[
        {"id": i, "title": q["title"], "rounds": len(q["rounds"]), "host_key": q["host_key"]}
        for i, q in QUIZZES.items()
    ])


@app.post("/api/quizzes")
def quiz_create():
    quiz, err = clean_quiz(request.json or {})
    if err:
        return jsonify(ok=False, error=err), 400
    qid = uuid.uuid4().hex[:8]
    quiz["host_key"] = secrets.token_urlsafe(8)
    with lock:
        QUIZZES[qid] = quiz
        save_quizzes(QUIZZES)
    return jsonify(ok=True, id=qid)


@app.get("/api/quizzes/<qid>")
def quiz_get(qid):
    get_quiz_or_404(qid)
    return jsonify(public_quiz(qid))


@app.put("/api/quizzes/<qid>")
def quiz_update(qid):
    old = get_quiz_or_404(qid)
    quiz, err = clean_quiz(request.json or {})
    if err:
        return jsonify(ok=False, error=err), 400
    quiz["host_key"] = old["host_key"]
    with lock:
        QUIZZES[qid] = quiz
        save_quizzes(QUIZZES)
    return jsonify(ok=True, id=qid)


@app.delete("/api/quizzes/<qid>")
def quiz_delete(qid):
    get_quiz_or_404(qid)
    with lock:
        QUIZZES.pop(qid, None)
        GAMES.pop(qid, None)
        save_quizzes(QUIZZES)
    return jsonify(ok=True)


# ---------- API: игра, участники ----------

@app.get("/api/q/<qid>/state")
def state(qid):
    q = get_quiz_or_404(qid)
    d = request.args.get("device", "")
    with lock:
        g = game(qid)
        r = g["r"]
        return jsonify(
            round=r + 1, total=len(q["rounds"]), open=g["open"],
            question=q["rounds"][r]["question"], answers=q["rounds"][r]["options"],
            voted=d in g["votes"].get(r, {}), session=g["session"], registered=d in g["players"],
        )


@app.post("/api/q/<qid>/join")
def join(qid):
    get_quiz_or_404(qid)
    x = request.json or {}
    d = str(x.get("device", ""))[:100]
    n = str(x.get("name", "")).strip()[:40]
    if not d or not n:
        return jsonify(ok=False), 400
    with lock:
        g = game(qid)
        g["players"][d] = n
        return jsonify(ok=True, session=g["session"])


@app.post("/api/q/<qid>/vote")
def vote(qid):
    q = get_quiz_or_404(qid)
    x = request.json or {}
    d = str(x.get("device", ""))
    try:
        c = int(x.get("choice", -1))
    except (TypeError, ValueError):
        return jsonify(ok=False), 400
    with lock:
        g = game(qid)
        if not g["open"]:
            return jsonify(ok=False, error="Голосование закрыто"), 409
        if d not in g["players"]:
            return jsonify(ok=False, error="Введите имя"), 403
        r = g["r"]
        if d in g["votes"].setdefault(r, {}):
            return jsonify(ok=False, error="Вы уже проголосовали"), 409
        if c not in range(len(q["rounds"][r]["options"])):
            return jsonify(ok=False), 400
        g["votes"][r][d] = c
    return jsonify(ok=True)


@app.get("/api/q/<qid>/leaders")
def leaders(qid):
    get_quiz_or_404(qid)
    with lock:
        g = game(qid)
        return jsonify(round=g["r"] + 1, leaders=leaderboard(qid))


# ---------- API: ведущий ----------

@app.get("/api/q/<qid>/admin")
def admin_state(qid):
    check_host(qid)
    q = QUIZZES[qid]
    with lock:
        g = game(qid)
        r = g["r"]
        opts = q["rounds"][r]["options"]
        counts = [0] * len(opts)
        for c in g["votes"].get(r, {}).values():
            if c < len(counts):
                counts[c] += 1
        return jsonify(
            round=r + 1, total=len(q["rounds"]), open=g["open"],
            question=q["rounds"][r]["question"], answers=opts,
            correct=q["rounds"][r]["correct"], counts=counts,
            players=len(g["players"]), voted=sum(counts), leaders=leaderboard(qid),
        )


@app.post("/api/q/<qid>/admin/action")
def admin_action(qid):
    check_host(qid)
    q = QUIZZES[qid]
    x = request.json or {}
    a = x.get("action")
    with lock:
        g = game(qid)
        if a == "toggle":
            g["open"] = not g["open"]
        elif a == "round":
            try:
                n = int(x["round"])
            except (KeyError, TypeError, ValueError):
                return jsonify(ok=False), 400
            g["r"] = max(0, min(len(q["rounds"]) - 1, n - 1))
            g["open"] = False
        elif a == "reset":
            GAMES[qid] = {"r": 0, "open": False, "session": str(uuid.uuid4()), "players": {}, "votes": {}}
        else:
            return jsonify(ok=False), 400
    return jsonify(ok=True)


# ---------- пульт презентаций (как в «Что было дальше») ----------

PRESENTATION_REMOTE_URL = os.environ.get(
    "PRESENTATION_REMOTE_URL", "https://presentation-remote-yy6x.onrender.com"
).rstrip("/")


def _remote_json(path, method="GET", payload=None):
    data, headers = None, {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(PRESENTATION_REMOTE_URL + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def _clean_login(v):
    return "".join(ch for ch in str(v or "").strip().lower() if ch.isalnum() or ch in "_-")[:40]


@app.post("/api/presentation/command")
def presentation_command():
    x = request.json or {}
    login, cmd = _clean_login(x.get("login")), str(x.get("command", ""))
    if len(login) < 2 or cmd not in ("next", "prev"):
        return jsonify(ok=False), 400
    try:
        return jsonify(_remote_json("/api/command", "POST", {"login": login, "command": cmd}))
    except Exception:
        return jsonify(ok=False, error="Presentation Remote unavailable"), 502


@app.get("/api/presentation/status")
def presentation_status():
    login = _clean_login(request.args.get("login"))
    if len(login) < 2:
        return jsonify(ok=True, online=0)
    try:
        return jsonify(_remote_json("/api/status?login=" + urllib.parse.quote(login)))
    except Exception:
        return jsonify(ok=False, online=0), 502


@app.post("/api/presentation/remote-heartbeat")
def presentation_remote_heartbeat():
    x = request.json or {}
    login, rid = _clean_login(x.get("login")), str(x.get("remote_id", ""))[:100]
    if len(login) < 2 or not rid:
        return jsonify(ok=False), 400
    try:
        return jsonify(_remote_json("/api/remote-heartbeat", "POST", {"login": login, "remote_id": rid}))
    except Exception:
        return jsonify(ok=False), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
