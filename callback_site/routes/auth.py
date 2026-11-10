"""First-run account setup, sign-in, and sign-out for the dashboard."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from flask import g, redirect, render_template, request, session, url_for

from services import admin_auth

logger = logging.getLogger(__name__)
FORM_EXPIRED = "The form expired. Try again."


def _page(mode: str, status: int = 200, **context: Any) -> Any:
    context.setdefault("error", "")
    context.setdefault("username", "")
    context.setdefault("next_path", "/")
    return render_template(
        "auth.html",
        mode=mode,
        min_password_length=admin_auth.MIN_PASSWORD_LENGTH,
        **context,
    ), status


def register_auth_routes(app: Any) -> None:
    app.config.update(
        SECRET_KEY=admin_auth.load_secret_key(),
        SESSION_COOKIE_NAME="playbook_admin",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=os.getenv("CALLBACK_SECURE_COOKIES", "").strip().lower() in {"1", "true", "yes"},
        PERMANENT_SESSION_LIFETIME=admin_auth.SESSION_LIFETIME,
    )
    app.before_request(admin_auth.gate_request)
    app.jinja_env.globals["csrf_token"] = admin_auth.csrf_token

    @app.context_processor
    def inject_admin_user() -> Dict[str, Any]:
        return {"admin_user": g.get("admin_user", "")}

    @app.route("/setup", methods=["GET", "POST"])
    def auth_setup() -> Any:
        if admin_auth.load_account() is not None:
            return redirect(url_for("auth_login"))
        if not admin_auth.setup_host_allowed():
            return _page(
                "setup",
                403,
                blocked=True,
                error="Run first-time setup from http://127.0.0.1 on the dashboard machine, "
                "or list this hostname in CALLBACK_ADMIN_HOSTS.",
            )
        if request.method == "GET":
            return _page("setup")
        username = request.form.get("username", "").strip()
        if not admin_auth.csrf_valid(request.form.get("csrf_token")):
            return _page("setup", 400, error=FORM_EXPIRED, username=username)
        error = admin_auth.validate_credentials(
            username, request.form.get("password", ""), request.form.get("confirm", "")
        )
        if error:
            return _page("setup", 400, error=error, username=username)
        account = admin_auth.create_account(username, request.form["password"])
        if account is None:
            return redirect(url_for("auth_login"))
        logger.info("Dashboard account %r created", username)
        admin_auth.start_session(account)
        return redirect("/")

    @app.route("/login", methods=["GET", "POST"])
    def auth_login() -> Any:
        account = admin_auth.load_account()
        if account is None:
            return redirect(url_for("auth_setup"))
        next_path = admin_auth.safe_next_path(request.values.get("next", "/"))
        if request.method == "GET":
            if admin_auth.current_user(account):
                return redirect(next_path)
            return _page("login", next_path=next_path)
        username = request.form.get("username", "").strip()
        if not admin_auth.csrf_valid(request.form.get("csrf_token")):
            return _page("login", 400, error=FORM_EXPIRED, username=username, next_path=next_path)
        wait = admin_auth.login_retry_after()
        if wait:
            return _page(
                "login",
                429,
                error=f"Too many failed sign-ins. Try again in {wait} seconds.",
                username=username,
                next_path=next_path,
            )
        signed_in = admin_auth.verify_login(username, request.form.get("password", ""))
        if signed_in is None:
            logger.warning("Failed dashboard sign-in for %r", username[:64])
            return _page("login", 401, error="Wrong username or password.", username=username, next_path=next_path)
        admin_auth.start_session(signed_in)
        logger.info("Dashboard sign-in for %r", signed_in["username"])
        return redirect(next_path)

    @app.post("/logout")
    def auth_logout() -> Any:
        session.clear()
        return redirect(url_for("auth_login"))
