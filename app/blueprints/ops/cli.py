import os
import re

import click
import psycopg2
from werkzeug.security import generate_password_hash

from app.blueprints.ops import totp
from app.blueprints.ops.security import enc


def _conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _password():
    pw = click.prompt("Password (at least 12 characters)", hide_input=True, confirmation_prompt=True)
    if len(pw) < 12:
        raise click.ClickException("Password must be at least 12 characters.")
    return pw


def _show(username, secret):
    spaced = " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))
    click.echo("\nAdd this to your authenticator app (Google/Microsoft Authenticator, Authy):")
    click.echo(f"  Account: {username}\n  Key (time-based): {spaced}")
    click.echo(f"  URI: {totp.uri(secret, username)}")
    click.echo("\nThis key is shown once. Store it somewhere safe.")


@click.command("ops-admin-create")
@click.argument("username")
def ops_admin_create(username):
    """Create an ops console administrator."""
    username = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{3,40}", username):
        raise click.ClickException("Username: 3-40 letters, digits, dot, dash or underscore.")
    password = _password()
    secret = totp.new_secret()
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("select 1 from ops_admins where lower(username)=%s", (username,))
            if cur.fetchone():
                raise click.ClickException("That username already exists.")
            cur.execute("insert into ops_admins (username, password_hash, totp_secret_enc) values (%s,%s,%s)",
                        (username, generate_password_hash(password), enc(secret)))
    finally:
        conn.close()
    click.echo(f"Created {username}.")
    _show(username, secret)


@click.command("ops-admin-reset")
@click.argument("username")
def ops_admin_reset(username):
    """Set a new password and a new authenticator key (lost phone, lockout)."""
    username = username.strip().lower()
    password = _password()
    secret = totp.new_secret()
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "update ops_admins set password_hash=%s, totp_secret_enc=%s, failed_attempts=0, "
                "locked_until=null, last_totp_step=null, active=true where lower(username)=%s",
                (generate_password_hash(password), enc(secret), username))
            if cur.rowcount != 1:
                raise click.ClickException("No such administrator.")
    finally:
        conn.close()
    click.echo(f"Reset {username}.")
    _show(username, secret)