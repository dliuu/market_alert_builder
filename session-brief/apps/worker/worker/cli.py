"""Worker CLI entrypoint. Real commands (backfill, brief) arrive in M2+."""

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(prog="worker")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("hello", help="smoke check that the worker boots")

    args = parser.parse_args()

    if args.command in (None, "hello"):
        print("worker: ok")


if __name__ == "__main__":
    main()
