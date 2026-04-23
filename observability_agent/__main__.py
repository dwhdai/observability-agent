"""CLI entry point — `python -m observability_agent [tui|agent]`."""
import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="observability-agent",
        description="SRE observability TUI + AI agent",
    )
    sub = parser.add_subparsers(dest="command")
    tui_parser = sub.add_parser("tui", help="Launch the TUI dashboard")
    tui_parser.add_argument(
        "--scenario",
        type=int,
        choices=[1, 2],
        help="1=Cascade Failure, 2=Memory Leak (default: random)",
    )
    sub.add_parser("agent", help="Launch the AI agent CLI")
    args = parser.parse_args()

    if args.command == "tui":
        from observability_agent.db import get_db_path, init_db
        init_db()
        from observability_agent.tui import run_tui
        run_tui(scenario=args.scenario)
    elif args.command == "agent":
        from observability_agent.db import get_db_path, init_db
        init_db()
        from observability_agent.agent import run_agent
        run_agent()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
