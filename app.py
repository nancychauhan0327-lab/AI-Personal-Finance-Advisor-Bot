import os, json, re, secrets
from datetime import date, datetime
from collections import defaultdict
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, flash, session, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()
app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY", "dev-change-me"),
    SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", "sqlite:///finance.db"),
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
db = SQLAlchemy(app)
lm = LoginManager(app)
lm.login_view = "login"

DEFAULT_CATS = ["Rent", "Food", "Transport", "Utilities", "Entertainment", "Health", "Shopping", "Other"]

# ---------- Models ----------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(60), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    savings_goal = db.Column(db.Float, default=0)      # target total savings
    goals = db.Column(db.String(300), default="")      # free-text financial goals

class Income(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    source = db.Column(db.String(80)); amount = db.Column(db.Float); date = db.Column(db.Date)

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    name = db.Column(db.String(60))

class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    category = db.Column(db.String(60)); amount = db.Column(db.Float)
    note = db.Column(db.String(200)); date = db.Column(db.Date)

class Budget(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    month = db.Column(db.String(7)); allocations = db.Column(db.Text, default="{}")
    advice = db.Column(db.Text, default=""); source = db.Column(db.String(20), default="rules")

class Saving(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    amount = db.Column(db.Float); note = db.Column(db.String(200)); date = db.Column(db.Date)

class Report(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    month = db.Column(db.String(7)); score = db.Column(db.Integer, default=0)
    summary = db.Column(db.Text, default=""); created = db.Column(db.DateTime, default=datetime.utcnow)

@lm.user_loader
def load_user(uid): return db.session.get(User, int(uid))

# ---------- Helpers ----------
def cur_month(): return request.args.get("m") or date.today().strftime("%Y-%m")

def month_bounds(m):
    try: y, mo = map(int, m.split("-")); s = date(y, mo, 1)
    except Exception: abort(400)
    e = date(y + (mo == 12), mo % 12 + 1, 1)
    return s, e

def num(v):
    try: x = float(v)
    except (TypeError, ValueError): return None
    return x if 0 < x < 1e9 else None

def pdate(v):
    try: return datetime.strptime(v, "%Y-%m-%d").date()
    except (TypeError, ValueError): return date.today()

def snapshot(m):
    s, e = month_bounds(m); uid = current_user.id
    inc = Income.query.filter(Income.user_id == uid, Income.date >= s, Income.date < e).all()
    exp = Expense.query.filter(Expense.user_id == uid, Expense.date >= s, Expense.date < e).all()
    sav = Saving.query.filter(Saving.user_id == uid, Saving.date >= s, Saving.date < e).all()
    by_cat = defaultdict(float)
    for x in exp: by_cat[x.category] += x.amount
    b = Budget.query.filter_by(user_id=uid, month=m).first()
    alloc = json.loads(b.allocations) if b else {}
    total_saved = sum(x.amount for x in Saving.query.filter_by(user_id=uid))
    d = dict(month=m, income=sum(x.amount for x in inc), expense=sum(x.amount for x in exp),
             saved=sum(x.amount for x in sav), by_cat=dict(by_cat), alloc=alloc, total_saved=total_saved)
    d["balance"] = d["income"] - d["expense"]
    d["rate"] = round(100 * (d["income"] - d["expense"]) / d["income"], 1) if d["income"] else 0
    d["over"] = {c: round(v - alloc[c], 2) for c, v in by_cat.items() if c in alloc and v > alloc[c]}
    return d

def health(d):
    """Rule-based financial health score (0-100) + findings."""
    score, notes = 50, []
    if d["income"] == 0: return 0, ["No income recorded for this month."]
    r = d["rate"]
    score += 25 if r >= 20 else 12 if r >= 10 else 0 if r >= 0 else -25
    notes.append(f"Savings rate is {r}% (target: 20%+).")
    score -= min(20, 7 * len(d["over"]))
    for c, o in d["over"].items(): notes.append(f"Overspent on {c} by {o:.0f}.")
    if d["expense"]:
        months_cover = d["total_saved"] / d["expense"]
        score += 15 if months_cover >= 3 else 7 if months_cover >= 1 else 0
        notes.append(f"Emergency fund covers {months_cover:.1f} months of expenses (target: 3-6).")
    return max(0, min(100, score)), notes

# ---------- AI layer ----------
def ask_ai(prompt):
    prov = os.getenv("AI_PROVIDER", "gemini").lower()
    try:
        if prov == "openai" and os.getenv("OPENAI_API_KEY"):
            from openai import OpenAI
            r = OpenAI().chat.completions.create(model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                                                 messages=[{"role": "user", "content": prompt}])
            return r.choices[0].message.content
        if prov == "gemini" and os.getenv("GEMINI_API_KEY"):
            import google.generativeai as genai
            genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
            return genai.GenerativeModel(os.getenv("GEMINI_MODEL", "gemini-2.0-flash")).generate_content(prompt).text
    except Exception as ex:
        app.logger.error("AI error: %s", ex)
    return None

def rule_budget(income, cats):
    w = {"Rent": 30, "Food": 15, "Transport": 8, "Utilities": 7, "Entertainment": 5, "Health": 5, "Shopping": 5, "Other": 5}
    base = {c: w.get(c, 4) for c in cats}; tot = sum(base.values()) or 1
    spend = income * 0.8  # keep 20% for savings
    return {c: round(spend * v / tot, 2) for c, v in base.items()}

def generate_budget(m):
    d = snapshot(m); cats = [c.name for c in Category.query.filter_by(user_id=current_user.id)]
    income = d["income"] or 0
    prompt = (f"You are a personal finance advisor. Monthly income: {income}. Categories: {cats}. "
              f"Recent spending by category: {d['by_cat']}. Goals: {current_user.goals or 'not stated'}. "
              f"Savings goal: {current_user.savings_goal}. Create a monthly budget that leaves at least 20% for savings. "
              'Reply ONLY with JSON: {"allocations": {"<category>": <amount>}, "advice": "<3-4 sentences>"}')
    raw = ask_ai(prompt)
    if raw:
        try:
            j = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
            alloc = {k: float(v) for k, v in j["allocations"].items() if k in cats}
            if alloc: return alloc, str(j.get("advice", "")), "ai"
        except Exception: app.logger.warning("Could not parse AI budget")
    return (rule_budget(income, cats),
            "Rule-based 80/20 plan: 20% of income reserved for savings, the rest spread across categories by typical weights. "
            "Add an AI API key in .env for personalised budgets.", "rules")

def generate_insights(d, score, notes):
    prompt = (f"Act as a personal finance advisor. Month {d['month']}: income {d['income']}, expenses {d['expense']}, "
              f"by category {d['by_cat']}, budget {d['alloc']}, total savings {d['total_saved']}, health score {score}/100. "
              "Give: 1) financial health evaluation, 2) overspending issues, 3) three cost-optimisation tips, "
              "4) savings and emergency-fund guidance. Be concise, plain text, bullet points.")
    return ask_ai(prompt) or ("(Rule-based insights)\n- " + "\n- ".join(notes) +
            "\n- Review your largest category for cuts; automate a monthly transfer to savings.")

# ---------- CSRF ----------
@app.before_request
def csrf():
    if request.method == "POST":
        if not session.get("_csrf") or request.form.get("_csrf") != session["_csrf"]: abort(400, "Bad CSRF token")

@app.context_processor
def inject():
    session.setdefault("_csrf", secrets.token_hex(16))
    return dict(csrf=session["_csrf"], month=request.args.get("m") or date.today().strftime("%Y-%m"))

# ---------- Auth ----------
@app.route("/")
def index(): return redirect(url_for("dashboard" if current_user.is_authenticated else "login"))

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        u, e, p = (request.form.get(k, "").strip() for k in ("username", "email", "password"))
        if not (u and "@" in e and len(p) >= 8): flash("Enter a username, valid email and 8+ char password.", "err")
        elif User.query.filter((User.username == u) | (User.email == e)).first(): flash("Username or email already used.", "err")
        else:
            user = User(username=u, email=e, password_hash=generate_password_hash(p))
            db.session.add(user); db.session.flush()
            db.session.add_all(Category(user_id=user.id, name=c) for c in DEFAULT_CATS); db.session.commit()
            login_user(user); return redirect(url_for("dashboard"))
    return render_template("auth.html", mode="register")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = User.query.filter_by(username=request.form.get("username", "").strip()).first()
        if u and check_password_hash(u.password_hash, request.form.get("password", "")):
            login_user(u); return redirect(url_for("dashboard"))
        flash("Invalid credentials.", "err")
    return render_template("auth.html", mode="login")

@app.route("/logout", methods=["POST"])
@login_required
def logout(): logout_user(); return redirect(url_for("login"))

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        current_user.savings_goal = num(request.form.get("savings_goal")) or 0
        current_user.goals = request.form.get("goals", "")[:300]
        name = request.form.get("category", "").strip()[:60]
        if name and not Category.query.filter_by(user_id=current_user.id, name=name).first():
            db.session.add(Category(user_id=current_user.id, name=name))
        db.session.commit(); flash("Profile updated.", "ok")
    cats = Category.query.filter_by(user_id=current_user.id).all()
    return render_template("profile.html", cats=cats)

@app.route("/category/<int:cid>/delete", methods=["POST"])
@login_required
def del_category(cid):
    c = Category.query.filter_by(id=cid, user_id=current_user.id).first_or_404()
    db.session.delete(c); db.session.commit(); return redirect(url_for("profile"))

# ---------- Generic record CRUD (income / expenses / savings) ----------
@app.route("/income", methods=["GET", "POST"])
@login_required
def income():
    m = cur_month()
    if request.method == "POST":
        a = num(request.form.get("amount"))
        if a and request.form.get("source", "").strip():
            db.session.add(Income(user_id=current_user.id, source=request.form["source"].strip()[:80], amount=a,
                                  date=pdate(request.form.get("date")))); db.session.commit()
        else: flash("Enter a source and a positive amount.", "err")
        return redirect(url_for("income", m=m))
    s, e = month_bounds(m)
    rows = Income.query.filter(Income.user_id == current_user.id, Income.date >= s, Income.date < e).order_by(Income.date.desc())
    return render_template("income.html", rows=rows, today=date.today())

@app.route("/expenses", methods=["GET", "POST"])
@login_required
def expenses():
    m = cur_month(); cats = [c.name for c in Category.query.filter_by(user_id=current_user.id)]
    if request.method == "POST":
        a, c = num(request.form.get("amount")), request.form.get("category")
        if a and c in cats:
            db.session.add(Expense(user_id=current_user.id, category=c, amount=a, note=request.form.get("note", "")[:200],
                                   date=pdate(request.form.get("date")))); db.session.commit()
        else: flash("Enter a positive amount and valid category.", "err")
        return redirect(url_for("expenses", m=m))
    s, e = month_bounds(m)
    rows = Expense.query.filter(Expense.user_id == current_user.id, Expense.date >= s, Expense.date < e).order_by(Expense.date.desc())
    return render_template("expenses.html", rows=rows, cats=cats, today=date.today())

@app.route("/savings", methods=["GET", "POST"])
@login_required
def savings():
    if request.method == "POST":
        a = num(request.form.get("amount"))
        if a:
            db.session.add(Saving(user_id=current_user.id, amount=a, note=request.form.get("note", "")[:200],
                                  date=pdate(request.form.get("date")))); db.session.commit()
        return redirect(url_for("savings"))
    rows = Saving.query.filter_by(user_id=current_user.id).order_by(Saving.date.desc()).all()
    total = sum(r.amount for r in rows); goal = current_user.savings_goal or 0
    pct = min(100, round(100 * total / goal)) if goal else 0
    return render_template("savings.html", rows=rows, total=total, goal=goal, pct=pct, today=date.today())

@app.route("/<kind>/<int:rid>/delete", methods=["POST"])
@login_required
def delete(kind, rid):
    model = {"income": Income, "expenses": Expense, "savings": Saving}.get(kind) or abort(404)
    r = model.query.filter_by(id=rid, user_id=current_user.id).first_or_404()
    db.session.delete(r); db.session.commit()
    return redirect(url_for(kind, m=request.args.get("m")))

# ---------- Budget / Dashboard / Report ----------
@app.route("/budget", methods=["GET", "POST"])
@login_required
def budget():
    m = cur_month()
    b = Budget.query.filter_by(user_id=current_user.id, month=m).first()
    if request.method == "POST":
        alloc, advice, src = generate_budget(m)
        if not b: b = Budget(user_id=current_user.id, month=m); db.session.add(b)
        b.allocations, b.advice, b.source = json.dumps(alloc), advice, src
        db.session.commit(); return redirect(url_for("budget", m=m))
    d = snapshot(m)
    return render_template("budget.html", b=b, alloc=d["alloc"], d=d)

@app.route("/dashboard")
@login_required
def dashboard():
    m = cur_month(); d = snapshot(m); score, notes = health(d)
    rep = Report.query.filter_by(user_id=current_user.id, month=m).first()
    return render_template("dashboard.html", d=d, score=score, notes=notes, rep=rep,
                           goal=current_user.savings_goal, cats=list(d["by_cat"]))

@app.route("/report", methods=["GET", "POST"])
@login_required
def report():
    m = cur_month(); d = snapshot(m); score, notes = health(d)
    rep = Report.query.filter_by(user_id=current_user.id, month=m).first()
    if request.method == "POST":
        text = generate_insights(d, score, notes)
        if not rep: rep = Report(user_id=current_user.id, month=m); db.session.add(rep)
        rep.score, rep.summary, rep.created = score, text, datetime.utcnow()
        db.session.commit(); return redirect(url_for("report", m=m))
    return render_template("report.html", d=d, score=score, notes=notes, rep=rep)

@app.errorhandler(400)
def bad(e): return render_template("auth.html", mode="error", err=str(e)), 400

with app.app_context(): db.create_all()

if __name__ == "__main__":
    if os.getenv("NGROK_AUTHTOKEN"):  # public URL for demos
        from pyngrok import ngrok
        ngrok.set_auth_token(os.getenv("NGROK_AUTHTOKEN"))
        print("\n PUBLIC URL:", ngrok.connect(5000).public_url, "\n")
    app.run(port=5000, debug=False)
