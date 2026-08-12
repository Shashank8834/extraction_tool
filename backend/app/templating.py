import os

from fastapi.templating import Jinja2Templates

from .config_loader import get_form_config

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

# Shared, CWD-independent templates instance.
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

# Branding is in form_config.yaml but the header/footer in base.html appear on
# every page — admin screens included — so expose it as a global rather than
# threading it through each route's context.
templates.env.globals["brand"] = get_form_config()["brand"]
