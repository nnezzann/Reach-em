"""Slack CLI entrypoint for the Reach application."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("reach_bot.app", run_name="__main__")
