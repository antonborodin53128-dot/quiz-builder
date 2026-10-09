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
SHOW_RESULTS = ("none", "question", "round")  # не показывать / после вопроса / в конце раунда
LETTERS = "АБВГДЕЖЗ"


# ---------- хранилище квизов ----------

def migrate(data):
    """Старый формат: раунд = один вопрос. Новый: раунд содержит список questions."""
    for quiz in data.values():
        quiz.setdefault("show_results", "none")
        quiz["rounds"] = [
            r if "questions" in r else {"questions": [{"question": r["question"], "options": r["options"], "correct": r["correct"]}]}
            for r in quiz["rounds"]
        ]
    return data


def load_quizzes():
    try:
        with open(QUIZ_FILE, encoding="utf-8") as f:
            return migrate(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_quizzes(data):
    tmp = QUIZ_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, QUIZ_FILE)


QUIZZES = load_quizzes()
GAMES = {}  # quiz_id -> состояние игры (в памяти)


def clean_question(r, where):
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
        return None, f"{where}: введите вопрос"
    if len(options) < MIN_OPTIONS:
        return None, f"{where}: нужно минимум {MIN_OPTIONS} варианта ответа"
    if correct is None:
        return None, f"{where}: отметьте правильный ответ"
    return {"question": question, "options": options, "correct": correct}, None


def clean_quiz(x):
    """Проверяет и нормализует квиз из запроса. Возвращает (quiz, error)."""
    title = str(x.get("title", "")).strip()[:80]
    if not title:
        return None, "Введите название квиза"
    rounds = []
    for i, r in enumerate(x.get("rounds") or [], 1):
        questions = []
        for j, qq in enumerate(r.get("questions") or [], 1):
            item, err = clean_question(qq, f"Раунд {i}, вопрос {j}")
            if err:
                return None, err
            questions.append(item)
        if not questions:
            return None, f"Раунд {i}: добавьте хотя бы один вопрос"
        rounds.append({"questions": questions})
    if not rounds:
        return None, "Добавьте хотя бы один раунд"
    show = x.get("show_results")
    return {"title": title, "rounds": rounds, "show_results": show if show in SHOW_RESULTS else "none"}, None


def flat_questions(qid):
    """Все вопросы подряд: (номер раунда, номер вопроса, вопросов в раунде, вопрос, подпись)."""
    rounds = QUIZZES[qid]["rounds"]
    out = []
    for ri, r in enumerate(rounds, 1):
        n = len(r["questions"])
        for qi, qq in enumerate(r["questions"], 1):
            # если раунд всего один — слово «раунд» участникам не показываем
            label = f"ВОПРОС {qi} ИЗ {n}" if len(rounds) == 1 else f"РАУНД {ri} · ВОПРОС {qi} ИЗ {n}"
            out.append((ri, qi, n, qq, label))
    return out


def public_quiz(qid):
    q = QUIZZES[qid]
    return {"id": qid, "title": q["title"], "rounds": q["rounds"], "host_key": q["host_key"],
            "show_results": q.get("show_results", "none")}


# ---------- состояние игры ----------

def new_game():
    # closed — вопросы, по которым голосование уже открывали и закрыли (после этого можно показать итог)
    return {"r": 0, "open": False, "session": str(uuid.uuid4()), "players": {}, "votes": {}, "closed": set()}


def game(qid):
    g = GAMES.get(qid)
    if g is None:
        g = GAMES[qid] = new_game()
    # квиз могли отредактировать — не выходим за границы
    g["r"] = min(g["r"], len(flat_questions(qid)) - 1)
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


def scores_by_device(qid, only=None):
    """Очки каждого участника. only — набор номеров вопросов (например, одного раунда)."""
    F, g = flat_questions(qid), GAMES[qid]
    scores = {d: 0 for d in g["players"]}
    for r, votes in g["votes"].items():
        if r >= len(F) or (only is not None and r not in only):
            continue
        for dev, c in votes.items():
            if dev in scores and c == F[r][3]["correct"]:
                scores[dev] += 1
    return scores


def leaderboard(qid):
    g = GAMES[qid]
    return sorted(
        ({"name": g["players"][d], "score": s} for d, s in scores_by_device(qid).items()),
        key=lambda x: (-x["score"], x["name"]),
    )


def place_of(scores, dev):
    """Место участника: одинаковые очки — одинаковое место."""
    return 1 + sum(1 for v in scores.values() if v > scores.get(dev, 0))


def participant_result(qid, dev):
    """Итог для телефона участника. None — пока показывать нечего или показ выключен."""
    mode = QUIZZES[qid].get("show_results", "none")
    g, F = GAMES[qid], flat_questions(qid)
    r = g["r"]
    if mode == "none" or g["open"] or r not in g["closed"] or dev not in g["players"]:
        return None
    ri, _, n, qq, _ = F[r]
    scores = scores_by_device(qid)
    if mode == "question":
        your = g["votes"].get(r, {}).get(dev)
        return {
            "type": "question", "correct": qq["correct"], "correct_text": qq["options"][qq["correct"]],
            "your": your, "right": None if your is None else your == qq["correct"],
            "score": scores.get(dev, 0), "place": place_of(scores, dev),
        }
    # mode == "round": итоги показываем после последнего вопроса раунда
    in_round = {i for i, f in enumerate(F) if f[0] == ri}
    if r != max(in_round):
        return None
    round_scores = scores_by_device(qid, in_round)
    top = sorted(scores.items(), key=lambda kv: (-kv[1], g["players"][kv[0]]))[:5]
    return {
        "type": "round", "round": ri, "right": round_scores.get(dev, 0), "of": len(in_round),
        "score": scores.get(dev, 0), "place": place_of(scores, dev),
        "top": [{"name": g["players"][d], "score": v, "place": place_of(scores, d)} for d, v in top],
    }


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
        {"id": i, "title": q["title"], "rounds": len(q["rounds"]),
         "questions": sum(len(r["questions"]) for r in q["rounds"]), "host_key": q["host_key"]}
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
    get_quiz_or_404(qid)
    d = request.args.get("device", "")
    with lock:
        g = game(qid)
        F = flat_questions(qid)
        r = g["r"]
        qq = F[r][3]
        return jsonify(
            label=F[r][4], round=F[r][0], total=len(F), open=g["open"],
            question=qq["question"], answers=qq["options"],
            voted=d in g["votes"].get(r, {}), session=g["session"], registered=d in g["players"],
            result=participant_result(qid, d),
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
    get_quiz_or_404(qid)
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
        if c not in range(len(flat_questions(qid)[r][3]["options"])):
            return jsonify(ok=False), 400
        g["votes"][r][d] = c
    return jsonify(ok=True)


@app.get("/api/q/<qid>/leaders")
def leaders(qid):
    get_quiz_or_404(qid)
    with lock:
        g = game(qid)
        return jsonify(label=flat_questions(qid)[g["r"]][4], leaders=leaderboard(qid))


# ---------- API: ведущий ----------

@app.get("/api/q/<qid>/admin")
def admin_state(qid):
    check_host(qid)
    with lock:
        g = game(qid)
        F = flat_questions(qid)
        r = g["r"]
        qq = F[r][3]
        counts = [0] * len(qq["options"])
        for c in g["votes"].get(r, {}).values():
            if c < len(counts):
                counts[c] += 1
        multi_round = len(QUIZZES[qid]["rounds"]) > 1
        items = [
            {"n": i + 1, "label": (f"Раунд {f[0]} · вопрос {f[1]}" if multi_round else f"Вопрос {f[1]}")}
            for i, f in enumerate(F)
        ]
        return jsonify(
            label=F[r][4], current=r + 1, total=len(F), items=items, open=g["open"],
            question=qq["question"], answers=qq["options"], correct=qq["correct"], counts=counts,
            players=len(g["players"]), voted=sum(counts), leaders=leaderboard(qid),
        )


@app.post("/api/q/<qid>/admin/action")
def admin_action(qid):
    check_host(qid)
    x = request.json or {}
    a = x.get("action")
    with lock:
        g = game(qid)
        if a == "toggle":
            g["open"] = not g["open"]
            if g["open"]:
                g["closed"].discard(g["r"])
            else:
                g["closed"].add(g["r"])
        elif a == "round":
            try:
                n = int(x["round"])
            except (KeyError, TypeError, ValueError):
                return jsonify(ok=False), 400
            g["r"] = max(0, min(len(flat_questions(qid)) - 1, n - 1))
            g["open"] = False
        elif a == "reset":
            GAMES[qid] = new_game()
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
