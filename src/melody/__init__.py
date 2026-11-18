from .application import MelodyApplication


def main():
    app = MelodyApplication()
    raise SystemExit(app.run())
