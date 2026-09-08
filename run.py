"""Local development entrypoint: python run.py"""
import os
from dotenv import load_dotenv

load_dotenv()

from dashboard.app import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_ENV") != "production"
    print(f"dashboard running at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
