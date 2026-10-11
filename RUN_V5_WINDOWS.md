# Run Expert AI Agent Pro Trader V5 on Windows

## Demo-first startup

1. Open MetaTrader 5 and log in to the intended demo account.
2. Keep Algo Trading enabled.
3. Activate the V5 virtual environment.
4. Install dependencies.
5. Copy `.env.example` to `.env` and keep `AGENT_MODE=DRY_RUN` initially.
6. Start `python main.py`.

```bat
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env
python tests\run_tests.py
python -m compileall -q .
python main.py
```

## Telegram

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`. The V5 Telegram Brain is optional and never blocks the trading loop on delivery failure.

## Direct OpenAI reasoning (optional)

Set:

```text
BRAIN__LLM_ENABLED=true
INTEL__PROVIDER=openai
INTEL__OPENAI_MODEL=<your model>
OPENAI_API_KEY=<your key>
```

The model is advisory only. It has no direct broker order tool.

## Safety modes

- `DRY_RUN`: analysis and simulation only.
- `DEMO`: real MT5 orders on a demo account.
- `LIVE`: real account; requires `CONFIRM_LIVE=YES_I_UNDERSTAND`.

Keep `BRAIN__ENFORCE=false` until the V5 brain has been validated in DRY_RUN and DEMO.

## VPS

For MT5 desktop + the Python package, use a Windows VPS. Install MT5 and Python on the VPS, copy the entire project directory, create the virtual environment, install requirements, configure `.env`, and launch `main.py`. Use Windows Task Scheduler or another process supervisor to start the application after reboot.


## V5.1 additions
1. `VERIFY_V5_WINDOWS.bat` now runs the full suite (expect `233 passed, 0 failed`) and validates your `.env`.
2. Heartbeat shows `event_label` (UPCOMING / PRE_EVENT / INITIAL_REACTION / ABSORPTION / POST_EVENT) and provider
   states: `NOT_PUBLISHED` for ForexFactory next week mid-week and `NOT_CONFIGURED` for key-based providers are normal.
3. If `calendar:bls_official` shows FAIL with 403, set `NEWS__HTTP_CONTACT=your@email` in `.env`.
4. Several markets: `START_MULTI_V5_WINDOWS.bat XAUUSD EURUSD US30` (max 5). Each market needs its own
   evidence report before demo.
5. Demo balance: with 0.5% risk, a $1,000 account usually cannot open gold's 0.01 minimum lot within budget;
   use a $10,000+ demo to test realistically.
6. Daily pre-market plan: `data\premarket\YYYY-MM-DD.txt` (and Telegram if configured).
