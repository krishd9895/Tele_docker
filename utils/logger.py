import sys
import logging
from pathlib import Path

def setup_structured_logging():
    log_format = "%(asctime)s [%(levelname)s] %(name)s [%(filename)s:%(lineno)d] - %(message)s"
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    
    if root_logger.hasHandlers():
        root_logger.handlers.clear()
        
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(log_format))
    root_logger.addHandler(console_handler)
    
    try:
        log_dir = Path("data/logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / "production_runtime.log", encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(log_format))
        file_handler.setLevel(logging.WARNING)
        root_logger.addHandler(file_handler)
    except Exception as e:
        print(f"CRITICAL: Failed to mount persistent log path: {e}", file=sys.stderr)

setup_structured_logging()