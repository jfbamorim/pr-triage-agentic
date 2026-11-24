import argparse

from dotenv import load_dotenv
from openai import OpenAI

from pr_triage.erp import ErpStore
from pr_triage.toolserver.server import build_server
from pr_triage.tools import PROFILES
from pr_triage.tools.policy import PolicyIndex


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m pr_triage.toolserver")
    parser.add_argument("--erp", default="state/erp.db")
    parser.add_argument("--profile", choices=PROFILES, default="agent")
    args = parser.parse_args()
    build_server(ErpStore(args.erp), PolicyIndex(OpenAI()), args.profile).run(show_banner=False)


if __name__ == "__main__":
    main()
