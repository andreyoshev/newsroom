"""OAuth 2.1 authorization server for the MCP connector.

The flow: Claude gets a 401 from /mcp -> reads discovery -> registers itself
(/oauth/register) -> /oauth/authorize (dashboard login plus feed picker) ->
/oauth/token. A grant is scoped to one feed: the token carries
(user_id, feed_id).

Every URL here is built from the configured public base URL, which is also the
issuer, so it is read per request rather than pinned at import time — an
operator can change the domain on /settings without restarting anything.
"""

from __future__ import annotations

import json
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from newsroom_shared import db as dbm
from newsroom_shared import oauth

from ..deps import load_user, render
from ..settings import base_url

router = APIRouter()


# --- discovery ---------------------------------------------------------------


def _protected_resource_meta(base: str) -> dict:
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
    }


def _authorization_server_meta(base: str) -> dict:
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp"],
    }


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/{rest:path}")
async def protected_resource_meta(request: Request, rest: str = ""):
    return JSONResponse(_protected_resource_meta(await base_url(request.scope)))


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/{rest:path}")
async def authorization_server_meta(request: Request, rest: str = ""):
    return JSONResponse(_authorization_server_meta(await base_url(request.scope)))


# --- dynamic client registration --------------------------------------------


@router.post("/oauth/register")
async def register(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)

    redirect_uris = body.get("redirect_uris") or []
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return JSONResponse(
            {"error": "invalid_redirect_uri", "error_description": "redirect_uris required"},
            status_code=400,
        )
    client_name = body.get("client_name") or "mcp-client"

    async with dbm.connect() as db:
        client_id = await oauth.register_client(
            db, client_name=client_name, redirect_uris_json=json.dumps(redirect_uris)
        )

    return JSONResponse(
        {
            "client_id": client_id,
            "client_name": client_name,
            "redirect_uris": redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        status_code=201,
    )


# --- authorize + consent -----------------------------------------------------


async def _valid_client(db, client_id: str, redirect_uri: str):
    client = await oauth.get_client(db, client_id)
    if client is None:
        return None
    try:
        uris = json.loads(client["redirect_uris"])
    except (TypeError, json.JSONDecodeError):
        return None
    return client if redirect_uri in uris else None


def _redirect_with(redirect_uri: str, **params: str) -> RedirectResponse:
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=303)


@router.get("/oauth/authorize")
async def authorize(request: Request):
    q = request.query_params
    client_id = q.get("client_id", "")
    redirect_uri = q.get("redirect_uri", "")
    code_challenge = q.get("code_challenge", "")
    state = q.get("state", "")

    if q.get("response_type") != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)
    if q.get("code_challenge_method", "S256") != "S256" or not code_challenge:
        return JSONResponse({"error": "invalid_request", "error_description": "PKCE S256 required"}, status_code=400)

    async with dbm.connect() as db:
        client = await _valid_client(db, client_id, redirect_uri)
    if client is None:
        return await render(request, "oauth_error.html", message="Unknown client or redirect_uri", status_code=400)

    user = await load_user(request)
    if user is None:
        next_url = f"/oauth/authorize?{request.url.query}"
        return RedirectResponse(f"/login?{urlencode({'next': next_url})}", status_code=303)

    async with dbm.connect() as db:
        feeds = await dbm.list_feeds(db, user["id"])

    return await render(
        request, "consent.html", user=user, feeds=feeds,
        client_name=client["client_name"] or "MCP client",
        params={
            "client_id": client_id, "redirect_uri": redirect_uri,
            "code_challenge": code_challenge, "state": state,
        },
    )


@router.post("/oauth/authorize")
async def authorize_submit(
    request: Request,
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    code_challenge: str = Form(...),
    state: str = Form(""),
    feed_id: str = Form(""),
    decision: str = Form("approve"),
):
    user = await load_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    async with dbm.connect() as db:
        client = await _valid_client(db, client_id, redirect_uri)
        if client is None:
            return await render(request, "oauth_error.html", message="Unknown client", status_code=400)

        if decision != "approve":
            return _redirect_with(redirect_uri, error="access_denied", state=state)

        feed = await dbm.get_feed(db, feed_id) if feed_id else None
        if feed is None or feed["user_id"] != user["id"]:
            return await render(request, "oauth_error.html", message="Select one of your feeds", status_code=400)

        code = await oauth.issue_code(
            db, client_id=client_id, user_id=user["id"], feed_id=feed_id,
            redirect_uri=redirect_uri, code_challenge=code_challenge,
        )

    return _redirect_with(redirect_uri, code=code, state=state)


# --- token -------------------------------------------------------------------


@router.post("/oauth/token")
async def token(
    request: Request,
    grant_type: str = Form(...),
    client_id: str = Form(""),
    code: str = Form(""),
    redirect_uri: str = Form(""),
    code_verifier: str = Form(""),
    refresh_token: str = Form(""),
):
    headers = {"Cache-Control": "no-store"}
    try:
        async with dbm.connect() as db:
            if grant_type == "authorization_code":
                result = await oauth.exchange_code(
                    db, code=code, client_id=client_id,
                    redirect_uri=redirect_uri, code_verifier=code_verifier,
                )
            elif grant_type == "refresh_token":
                result = await oauth.refresh_tokens(
                    db, refresh_token=refresh_token, client_id=client_id
                )
            else:
                return JSONResponse({"error": "unsupported_grant_type"}, status_code=400, headers=headers)
    except oauth.OAuthError as exc:
        return JSONResponse(
            {"error": exc.error, "error_description": exc.description},
            status_code=400, headers=headers,
        )
    return JSONResponse(result, headers=headers)
