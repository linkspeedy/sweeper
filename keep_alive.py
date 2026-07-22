import os
from threading import Thread

from flask import Flask

app = Flask('')


@app.route('/')
def home():
    return "Sweeper worker is alive and running!"


def run():
    # Render binds dynamic ports via the PORT environment variable.
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)


def keep_alive():
    t = Thread(target=run)
    t.start()
