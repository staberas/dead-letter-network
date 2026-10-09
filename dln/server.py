from .api import create_apps
from .config import Settings

public, admin = create_apps(Settings.from_env())
