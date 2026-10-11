import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import VERSION, Config


def setup_logging(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(),
                                  RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5)])
    import redact
    redact.install()


def main():
    cfg = Config.from_env()
    setup_logging(cfg.system.log_file)
    log = logging.getLogger("main")
    log.info("Expert AI Agent Pro Trader v%s starting in %s mode (symbol=%s)", VERSION, cfg.system.mode, cfg.system.symbol)
    errors = cfg.validate()
    if errors:
        for e in errors:
            log.error("config error: %s", e)
        sys.exit(2)
    try:
        from engine import AgentEngine      # imported late so config errors do not need MT5 installed
        from mt5_client import MT5Client
        from learning import apply_production
    except ImportError as ex:
        log.error("cannot start: %s. The MetaTrader5 package only runs on Windows with the MT5 terminal "
                  "installed (pip install -r requirements.txt).", ex)
        sys.exit(3)
    cfg, pv = apply_production(cfg)
    if pv:
        log.info("approved production parameters %s applied", pv)
    try:
        AgentEngine(cfg, MT5Client(cfg)).run()
    except Exception:
        log.exception("fatal error during startup or shutdown")
        sys.exit(1)


if __name__ == "__main__":
    main()
