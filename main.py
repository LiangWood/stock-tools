import os
import logging

os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")

from ui.app import App

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

if __name__ == "__main__":
    app = App()
    app.mainloop()
