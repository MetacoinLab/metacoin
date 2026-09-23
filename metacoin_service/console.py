"""Mounts the server-rendered console (console_ui) onto the API application."""
def mount(app, svc):
    from . import console_ui
    console_ui.mount(app, svc)
