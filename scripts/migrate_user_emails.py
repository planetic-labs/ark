"""Export or import Ark user email addresses without starting an app container.

Install the only dependency once with ``python3 -m pip install asyncpg``. The
export contains only email addresses. Import creates missing users with the
selected role; it does not copy passwords, tokens, messages, or profile data.
"""

import argparse
import asyncio
import json
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

PROJECT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser("export", help="Export active user emails to JSON.")
    export.add_argument(
        "--source-database-url",
        help="PostgreSQL URL of the database to read from (default: DATABASE_URL in .env).",
    )
    export.add_argument("--output", type=Path, required=True, help="Output JSON file.")

    import_ = commands.add_parser("import", help="Import emails from a JSON export.")
    import_.add_argument("--input", type=Path, required=True, help="Input JSON file.")
    import_.add_argument(
        "--target-database-url",
        help="PostgreSQL URL of the database to write to (default: DATABASE_URL in .env).",
    )
    import_.add_argument(
        "--role",
        default="student",
        help="Role assigned to imported users (default: student).",
    )
    import_.add_argument(
        "--apply",
        action="store_true",
        help="Create users in the target database. Without this flag, only preview.",
    )
    import_.add_argument(
        "--verbose",
        action="store_true",
        help="Print the email address of every user that would be created.",
    )
    return parser.parse_args()


def database_url(url: str, option: str) -> str:
    scheme, separator, rest = url.partition("://")
    if separator and scheme.startswith("postgresql+"):
        url = f"postgresql://{rest}"
    if not url.startswith(("postgresql://", "postgres://")):
        raise ValueError(f"{option} must start with postgresql:// or postgres://")
    parsed = urlsplit(url)
    if parsed.hostname != "db":
        return url

    credentials, _, host = parsed.netloc.rpartition("@")
    host = host.replace("db", "127.0.0.1", 1)
    netloc = f"{credentials}@{host}" if credentials else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def env_file_path() -> Path:
    current_directory_env = Path.cwd() / ".env"
    if current_directory_env.exists():
        return current_directory_env
    return PROJECT_ENV_FILE


def read_env_values() -> dict[str, str]:
    env_file = env_file_path()
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError(f"Cannot read {env_file}: {error}") from error

    values = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, separator, value = line.partition("=")
        if separator:
            value = value.strip()
            if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
                value = value[1:-1]
            if value:
                values[key.strip()] = value
    return values


def database_url_from_env() -> str:
    values = read_env_values()
    if database_url := values.get("DATABASE_URL"):
        return database_url

    user = values.get("POSTGRES_USER", "postgres")
    password = values.get("POSTGRES_PASSWORD")
    database_name = values.get("POSTGRES_DB")
    if password and database_name:
        host = values.get("POSTGRES_HOST", "127.0.0.1")
        port = values.get("POSTGRES_PORT", "5432")
        return (
            f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
            f"@{host}:{port}/{quote(database_name, safe='')}"
        )

    raise ValueError(
        f"DATABASE_URL or POSTGRES_PASSWORD and POSTGRES_DB are not set in "
        f"{env_file_path()}"
    )


def normalize_emails(emails: list[object]) -> list[str]:
    return list(
        dict.fromkeys(
            email.strip().lower()
            for email in emails
            if isinstance(email, str) and email.strip()
        )
    )


def new_ulid() -> str:
    """Return a 26-character Crockford Base32 ULID using only stdlib."""
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    value = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    result = ""
    for _ in range(26):
        result = alphabet[value & 31] + result
        value >>= 5
    return result


async def fetch_source_emails(asyncpg: object, source_url: str) -> list[str]:
    connection = await asyncpg.connect(source_url)
    try:
        rows = await connection.fetch(
            "SELECT email FROM users WHERE deleted_at IS NULL ORDER BY email"
        )
        return normalize_emails([row["email"] for row in rows])
    finally:
        await connection.close()


async def export_emails(asyncpg: object, args: argparse.Namespace) -> None:
    emails = await fetch_source_emails(asyncpg, args.source_database_url)
    payload = {"format_version": 1, "emails": emails}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Exported {len(emails)} active unique email(s) to {args.output}.")


def load_emails(input_path: Path) -> list[str]:
    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"Cannot read {input_path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in {input_path}: {error}") from error

    if not isinstance(payload, dict) or payload.get("format_version") != 1:
        raise ValueError("Input file must be an email export with format_version 1.")
    emails = payload.get("emails")
    if not isinstance(emails, list):
        raise ValueError("Input file field 'emails' must be a list.")
    return normalize_emails(emails)


async def import_emails(asyncpg: object, args: argparse.Namespace) -> None:
    source_emails = load_emails(args.input)
    connection = await asyncpg.connect(args.target_database_url)
    try:
        role_id = await connection.fetchval(
            "SELECT id FROM roles WHERE name = $1", args.role
        )
        role_missing = role_id is None

        existing_emails = normalize_emails(
            [row["email"] for row in await connection.fetch("SELECT email FROM users")]
        )
        existing_email_set = set(existing_emails)
        emails_to_create = [
            email for email in source_emails if email not in existing_email_set
        ]
        print(
            f"File contains: {len(source_emails)} unique email(s); "
            f"target already contains: {len(existing_emails)}; "
            f"to create: {len(emails_to_create)}."
        )
        if role_missing:
            print(f"Role {args.role!r} will also be created.")
        if args.verbose:
            for email in emails_to_create:
                print(email)

        if not args.apply:
            print("Dry run complete. Re-run with --apply to create these users.")
            return

        async with connection.transaction():
            if role_missing:
                role_id = new_ulid()
                await connection.execute(
                    "INSERT INTO roles (id, name, is_system, is_default) "
                    "VALUES ($1, $2, FALSE, TRUE)",
                    role_id,
                    args.role,
                )
            for email in emails_to_create:
                user_id = new_ulid()
                await connection.execute(
                    "INSERT INTO users "
                    "(id, email, is_active, is_approved, first_name, last_name, "
                    "status, email_verified) "
                    "VALUES ($1, $2, TRUE, TRUE, $3, '', 'active', TRUE)",
                    user_id,
                    email,
                    email.partition("@")[0].capitalize(),
                )
                await connection.execute(
                    "INSERT INTO user_roles (user_id, role_id) VALUES ($1, $2)",
                    user_id,
                    role_id,
                )
        print(f"Created {len(emails_to_create)} user(s).")
    finally:
        await connection.close()


async def main() -> int:
    args = parse_args()
    try:
        import asyncpg
    except ImportError:
        print("Install dependency first: python3 -m pip install asyncpg", file=sys.stderr)
        return 1

    try:
        if args.command == "export":
            args.source_database_url = database_url(
                args.source_database_url or database_url_from_env(),
                "--source-database-url or DATABASE_URL",
            )
            await export_emails(asyncpg, args)
        else:
            args.target_database_url = database_url(
                args.target_database_url or database_url_from_env(),
                "--target-database-url or DATABASE_URL",
            )
            await import_emails(asyncpg, args)
    except (OSError, RuntimeError, ValueError, asyncpg.PostgresError) as error:
        print(f"Migration failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
