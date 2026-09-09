"""백오피스 진입점.

실행:  python run_backoffice.py   →  http://127.0.0.1:8000  (ALERT_BACKOFFICE_HOST / PORT 로 변경)
"""

import uvicorn

from alertbot.config import BACKOFFICE_HOST, BACKOFFICE_PORT, setup_logging

if __name__ == "__main__":
    setup_logging(None)
    uvicorn.run("alertbot.backoffice.app:app", host=BACKOFFICE_HOST, port=BACKOFFICE_PORT, log_level="info")
