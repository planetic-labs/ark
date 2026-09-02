"""Export or import Ark user email addresses.

The export contains only email addresses. Import creates missing users with the
selected role; it does not copy passwords, tokens, messages, or profile data.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modules.users.models import Role, User


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser("export", help="Export active user emails to JSON.")
    export.add_argument(
        "--source-database-url",
        required=True,
        help="SQLAlchemy asyncpg URL of the database to read from.",
    )
    export.add_argument("--output", type=Path, required=True, help="Output JSON file.")

    import_ = commands.add_parser("import", help="Import emails from a JSON export.")
    import_.add_argument("--input", type=Path, required=True, help="Input JSON file.")
    import_.add_argument(
        "--target-database-url",
        required=True,
        help="SQLAlchemy asyncpg URL of the database to write to.",
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


def validate_database_url(url: str, option: str) -> None:
    if not url.startswith("postgresql+asyncpg://"):
        raise ValueError(f"{option} must start with postgresql+asyncpg://")


def normalize_emails(emails: list[object]) -> list[str]:
    return list(
        dict.fromkeys(
            email.strip().lower()
            for email in emails
            if isinstance(email, str) and email.strip()
        )
    )


async def fetch_source_emails(source_url: str) -> list[str]:
    engine = create_async_engine(source_url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                sa.text(
                    "SELECT email FROM users "
                    "WHERE deleted_at IS NULL ORDER BY email"
                )
            )
            return normalize_emails(list(result.scalars()))
    finally:
        await engine.dispose()


async def export_emails(args: argparse.Namespace) -> None:
    emails = await fetch_source_emails(args.source_database_url)
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


async def import_emails(args: argparse.Namespace) -> None:
    source_emails = load_emails(args.input)
    target_engine = create_async_engine(args.target_database_url)
    session_factory = async_sessionmaker(target_engine, class_=AsyncSession)
    try:
        async with session_factory() as session:
            role_result = await session.execute(
                sa.select(Role).where(Role.name == args.role)
            )
            role = role_result.scalar_one_or_none()
            if role is None:
                raise RuntimeError(
                    f"Role {args.role!r} is absent in the target database. "
                    "Run Alembic migrations and start the API once first."
                )

            existing_result = await session.execute(sa.select(User.email))
            existing_emails = normalize_emails(list(existing_result.scalars()))
            emails_to_create = [
                email for email in source_emails if email not in set(existing_emails)
            ]
            print(
                f"File contains: {len(source_emails)} unique email(s); "
                f"target already contains: {len(existing_emails)}; "
                f"to create: {len(emails_to_create)}."
            )
            if args.verbose:
                for email in emails_to_create:
                    print(email)

            if not args.apply:
                print("Dry run complete. Re-run with --apply to create these users.")
                return

            for email in emails_to_create:
                user = User(
                    email=email,
                    is_active=True,
                    is_approved=True,
                    email_verified=True,
                    status="active",
                )
                user.full_name = email.partition("@")[0].capitalize()
                user.roles.append(role)
                session.add(user)
            await session.commit()
            print(f"Created {len(emails_to_create)} user(s).")
    finally:
        await target_engine.dispose()


async def main() -> int:
    args = parse_args()
    try:
        if args.command == "export":
            validate_database_url(args.source_database_url, "--source-database-url")
            await export_emails(args)
        else:
            validate_database_url(args.target_database_url, "--target-database-url")
            await import_emails(args)
    except (ValueError, RuntimeError, sa.SQLAlchemyError) as error:
        print(f"Migration failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
