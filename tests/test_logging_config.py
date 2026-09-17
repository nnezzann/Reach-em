import logging

from reach_bot.logging_config import configure_logging


def test_file_logging_is_timestamped_without_message_content(tmp_path) -> None:
    log_path = tmp_path / "nested" / "reach.log"
    configure_logging("INFO", str(log_path))
    logging.getLogger("test").info("startup complete xoxb-secret nvapi-secret")

    line = log_path.read_text()
    assert line.endswith("INFO test startup complete [REDACTED] [REDACTED]\n")
    assert "xoxb-secret" not in line
    assert "nvapi-secret" not in line
    assert len(line[:4]) == 4 and line[:4].isdigit()
