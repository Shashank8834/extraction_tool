import os

from fastapi.templating import Jinja2Templates

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

# Shared, CWD-independent templates instance.
templates = Jinja2Templates(directory=_TEMPLATES_DIR)
