"""AWS credential resolution for DeployEval.

Policy (per project owner): use AWS_PROFILE=personal first; if that fails to authenticate,
fall back to the AWS_* keys in credentials.env. Never print secret values.

All deploy/teardown/free-tier code calls resolve_session() so credential handling lives in
exactly one place.
"""

from __future__ import annotations

import os
from pathlib import Path

PROFILE = "personal"
# credentials.env lives at the repo root (gitignored). Fallback only.
CREDS_ENV = Path(__file__).resolve().parents[2] / "credentials.env"


def _load_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines from a .env file (ignores comments/blanks). No logging of values."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def resolve_session(region: str | None = None):
    """Return an authenticated boto3 Session, trying the profile then the env-file fallback.

    Raises RuntimeError with a safe message (no secrets) if neither works.
    """
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    region = region or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"

    # 1) AWS_PROFILE=personal
    try:
        sess = boto3.Session(profile_name=PROFILE, region_name=region)
        ident = sess.client("sts").get_caller_identity()
        _print_identity("profile:personal", ident)
        return sess
    except (BotoCoreError, ClientError, Exception):  # noqa: BLE001 — fall through to fallback
        pass

    # 2) Fallback: AWS_* keys from credentials.env
    env = _load_env_file(CREDS_ENV)
    ak, sk = env.get("AWS_ACCESS_KEY_ID"), env.get("AWS_SECRET_ACCESS_KEY")
    if ak and sk:
        try:
            sess = boto3.Session(
                aws_access_key_id=ak,
                aws_secret_access_key=sk,
                region_name=env.get("AWS_DEFAULT_REGION", region),
            )
            ident = sess.client("sts").get_caller_identity()
            _print_identity("credentials.env fallback", ident)
            return sess
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"credentials.env AWS keys failed to authenticate: {exc!r}") from exc

    raise RuntimeError(
        "No working AWS credentials. Set AWS_PROFILE=personal (recommended) or put "
        "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY in credentials.env."
    )


def _print_identity(source: str, ident: dict) -> None:
    """Print the resolved account so every run states where it deployed (account id is not secret)."""
    print(f"[deployeval] AWS via {source} -> account {ident.get('Account')} "
          f"arn {ident.get('Arn')}")
