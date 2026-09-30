# AI Personal Finance Advisor (Flask)
```
python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env     # set SECRET_KEY, and GEMINI_API_KEY or OPENAI_API_KEY (+ AI_PROVIDER)
python app.py            # http://127.0.0.1:5000
```
**Public URL (Ngrok):** put `NGROK_AUTHTOKEN` in `.env` and run `python app.py`; the public URL is printed.
**PostgreSQL:** set `DATABASE_URL=postgresql://user:pass@host/db`.
No AI key? Budgets and insights fall back to rule-based logic.
