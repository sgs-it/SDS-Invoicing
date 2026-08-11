"""SDS Invoice & Client Payment Tracking System — entry point.

Run with:  python app.py
Then open http://localhost:5000 and sign in with the seeded admin account
(username: admin, password: admin123). You will be prompted to change it.
"""
from web import create_app

app = create_app()

if __name__ == "__main__":
    # Debug off by default; set SDS_DEBUG=1 to enable the reloader.
    import os
    debug = os.environ.get("SDS_DEBUG") == "1"
    app.run(host="127.0.0.1", port=5000, debug=debug)
