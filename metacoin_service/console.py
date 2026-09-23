"""Placeholder mount; the console is defined in console_ui.py (written next)."""
def mount(app, svc):
    try:
        from . import console_ui
        console_ui.mount(app, svc)
    except ImportError:
        pass
