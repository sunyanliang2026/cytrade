"""Run MainSealFollowStrategy with local runtime configuration."""

from strategies.main_seal_follow import MainSealFollowStrategy
from main import run_scheduler_service


def main() -> None:
    run_scheduler_service(strategy_classes=[MainSealFollowStrategy])


if __name__ == "__main__":
    main()
