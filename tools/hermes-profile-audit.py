#!/usr/bin/env python3
"""Audit Hermes Agent profiles for account-connection (credential) wiring.

Read-only. Never prints secret values — only key names, whether a value is
present, and an 8-hex-char SHA-256 fingerprint so you can tell whether two
profiles carry the SAME credential without revealing either.

Usage:
    python3 tools/hermes-profile-audit.py [--root ~/.hermes] [--json]

Why this exists
---------------
In Hermes, "account connection" is per profile, and it lives in three
separate places that resolve independently:

  1. <profile>/.env          -> provider API keys / platform bot tokens
  2. <profile>/auth.json     -> OAuth / device-code credential pools
                                + `active_provider`
  3. <profile>/config.yaml   -> model.provider / model.default

`hermes profile create <name>` (without --clone) seeds an EMPTY .env — a
comment-only placeholder. The profile therefore shows up as configured in
`hermes profile show` (".env: exists") while holding zero credentials.
Under `gateway.multiplex_profiles: true` that profile's secret scope is
authoritative (agent/secret_scope.py), so it cannot fall back to the shell
environment or to the root ~/.hermes/.env — and the profile silently does
nothing. This script makes that state visible.

OAuth setups fail differently. Hermes keeps a SINGLE install-wide Nous OAuth
store at <root>/shared/nous_auth.json with no account identity attached
(hermes_cli/auth.py::_merge_shared_nous_oauth_state). Every Nous token
resolution merges that store over the profile's own tokens whenever its
refresh token differs, so profiles configured with DIFFERENT Nous accounts
converge onto whichever account refreshed last; the losers replay a rotated
single-use refresh token and die with invalid_grant / refresh_token_reused.
This script decodes each profile's OAuth access token (locally, no network,
no token ever printed) and reports the account identity behind it, so an
account collision is visible at a glance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any

PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Key names that carry an account credential. Matched case-insensitively as a
# substring, except SECRET_* style prefixes which are matched as-is.
CRED_HINTS = (
    "API_KEY", "APIKEY", "_KEY", "TOKEN", "SECRET", "PASSWORD",
    "CLIENT_ID", "CLIENT_SECRET", "ACCESS_KEY", "CREDENTIAL",
)

# Env keys that on their own let the "auto" provider path resolve.
GENERIC_INFERENCE_KEYS = ("OPENAI_API_KEY", "OPENROUTER_API_KEY")


# ---------------------------------------------------------------------------
# OAuth inspection
# ---------------------------------------------------------------------------

# Claims worth showing. Everything else in the payload is ignored so a token
# body can never be dumped wholesale.
IDENTITY_CLAIMS = ("sub", "email", "preferred_username", "name", "org_id",
                   "organization_id", "account_id", "user_id", "azp", "iss")

# Entitlement claims Hermes itself reads to decide whether an account may use
# paid inference (hermes_cli/nous_account.py::_info_from_valid_jwt). When these
# disagree with what the portal shows in a browser, the stored token is stale
# or belongs to a different account than the one you think is connected.
ENTITLEMENT_CLAIMS = ("paid_access", "subscription_tier", "product_id",
                      "nous_client", "tool_access", "scope")

OAUTH_FIELDS = ("access_token", "refresh_token", "id_token", "expires_at",
                "obtained_at", "auth_type", "portal_base_url")


def decode_jwt_claims(token: str) -> dict[str, Any] | None:
    """Decode a JWT payload locally. No signature check, no network."""
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        import base64

        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def identity_of(state: dict[str, Any]) -> dict[str, Any]:
    """Summarize one OAuth credential: whose account, when it expires."""
    out: dict[str, Any] = {}
    access = state.get("access_token")
    refresh = state.get("refresh_token")

    out["has_access_token"] = bool(access)
    out["has_refresh_token"] = bool(refresh)
    out["access_fp"] = fingerprint(access) if isinstance(access, str) and access else None
    out["refresh_fp"] = fingerprint(refresh) if isinstance(refresh, str) and refresh else None

    claims = decode_jwt_claims(access) if isinstance(access, str) else None
    if claims:
        out["identity"] = {k: claims[k] for k in IDENTITY_CLAIMS if k in claims}
        entitlement = {k: claims[k] for k in ENTITLEMENT_CLAIMS if k in claims}
        if entitlement:
            out["entitlement"] = entitlement
        exp = claims.get("exp")
        if isinstance(exp, (int, float)):
            out["exp_epoch"] = int(exp)
            out["expired"] = exp < time.time()
    if "expired" not in out and state.get("expires_at"):
        parsed = parse_iso(str(state["expires_at"]))
        if parsed is not None:
            out["exp_epoch"] = int(parsed)
            out["expired"] = parsed < time.time()
    out["expires_at"] = state.get("expires_at")
    out["last_status"] = state.get("last_status")
    out["last_error_code"] = state.get("last_error_code")
    out["last_error_reason"] = state.get("last_error_reason")
    out["last_error_message"] = state.get("last_error_message")

    # A quota failure benches the credential until last_error_reset_at, a
    # timestamp the SERVER supplied. Hermes will not retry before then even if
    # the quota has already refilled (agent/credential_pool.py::_exhausted_until).
    reset_at = state.get("last_error_reset_at")
    if reset_at is not None:
        epoch = reset_at if isinstance(reset_at, (int, float)) else parse_iso(str(reset_at))
        if epoch is not None:
            out["cooldown_until_epoch"] = int(epoch)
            out["cooldown_remaining_s"] = int(epoch - time.time())
    return out


def parse_iso(value: str) -> float | None:
    try:
        from datetime import datetime

        text = value.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return None


def collect_oauth(auth: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Return {provider: [credential summary, ...]} from an auth.json dict."""
    found: dict[str, list[dict[str, Any]]] = {}

    providers = auth.get("providers")
    if isinstance(providers, dict):
        for name, state in providers.items():
            if not isinstance(state, dict):
                continue
            if not any(state.get(f) for f in ("access_token", "refresh_token")):
                continue
            summary = identity_of(state)
            summary["slot"] = "providers"
            found.setdefault(name, []).append(summary)

    pool = auth.get("credential_pool")
    if isinstance(pool, dict):
        for name, entries in pool.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if not any(entry.get(f) for f in ("access_token", "refresh_token")):
                    continue
                summary = identity_of(entry)
                summary["slot"] = f"credential_pool[{entry.get('id') or '?'}]"
                found.setdefault(name, []).append(summary)

    return found


def describe_identity(summary: dict[str, Any]) -> str:
    """One-line, secret-free description of whose account a credential is."""
    ident = summary.get("identity") or {}
    who = (ident.get("email") or ident.get("preferred_username")
           or ident.get("sub") or ident.get("account_id") or ident.get("user_id"))
    if not who:
        return "account unknown (token is not a JWT or carries no identity claim)"
    return str(who)


def fingerprint(value: str) -> str:
    """Short, non-reversible fingerprint used to compare keys across profiles."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def is_credential_key(key: str) -> bool:
    upper = key.upper()
    return any(hint in upper for hint in CRED_HINTS)


def parse_dotenv(path: Path) -> dict[str, str]:
    """Parse the KEY=VALUE subset Hermes writes. Mirrors agent/secret_scope.py."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def read_yaml(path: Path) -> dict[str, Any]:
    """Best-effort YAML read; falls back to a tiny scanner when PyYAML is absent."""
    if not path.is_file():
        return {}
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except ImportError:
        pass
    except Exception:
        return {}
    # Minimal fallback: only the two-level keys this audit needs.
    data: dict[str, Any] = {}
    section: str | None = None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if indent == 0:
            section = key if not value else None
            if value:
                data[key] = _scalar(value)
            else:
                data.setdefault(key, {})
        elif section and isinstance(data.get(section), dict) and value:
            data[section][key] = _scalar(value)
    return data


def _scalar(value: str) -> Any:
    low = value.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    return value.strip("'\"")


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def model_config(cfg: dict[str, Any]) -> tuple[str | None, str | None]:
    model = cfg.get("model")
    if isinstance(model, str):
        return model, None
    if isinstance(model, dict):
        return model.get("default") or model.get("model"), model.get("provider")
    return None, None


def gateway_flags(cfg: dict[str, Any]) -> dict[str, Any]:
    """Read multiplexing/routing flags from either the flat or nested form."""
    gw = cfg.get("gateway") if isinstance(cfg.get("gateway"), dict) else {}
    multiplex = cfg.get("multiplex_profiles")
    if multiplex is None:
        multiplex = gw.get("multiplex_profiles")
    allowlist = cfg.get("multiplex_profile_allowlist")
    if allowlist is None:
        allowlist = gw.get("multiplex_profile_allowlist")
    routes = cfg.get("profile_routes")
    if routes is None:
        routes = gw.get("profile_routes")
    env_override = os.environ.get("HERMES_GATEWAY_MULTIPLEX_PROFILES")
    return {
        "multiplex_profiles": bool(multiplex),
        "multiplex_profile_allowlist": allowlist if isinstance(allowlist, list) else None,
        "profile_routes": routes if isinstance(routes, list) else [],
        "env_override": env_override,
    }


def inspect_profile(name: str, home: Path, *, is_default: bool) -> dict[str, Any]:
    env_path = home / ".env"
    env_vars = parse_dotenv(env_path) if env_path.is_file() else {}
    cred_keys = {k: v for k, v in env_vars.items() if is_credential_key(k)}

    auth = read_json(home / "auth.json")
    providers = auth.get("providers")
    provider_names = sorted(providers.keys()) if isinstance(providers, dict) else []
    oauth = collect_oauth(auth)

    cfg = read_yaml(home / "config.yaml")
    model, provider = model_config(cfg)

    meta = read_yaml(home / "profile.yaml")
    mode = None
    if env_path.is_file():
        try:
            mode = stat.S_IMODE(env_path.stat().st_mode)
        except OSError:
            mode = None

    return {
        "name": name,
        "display_name": str(meta.get("display_name") or "").strip(),
        "path": str(home),
        "is_default": is_default,
        "valid_id": name == "default" or bool(PROFILE_ID_RE.match(name)),
        "env_exists": env_path.is_file(),
        "env_mode": f"{mode:04o}" if mode is not None else None,
        "env_key_count": len(env_vars),
        "credential_keys": {
            k: {"set": bool(v), "fp": fingerprint(v) if v else None}
            for k, v in sorted(cred_keys.items())
        },
        "auth_exists": (home / "auth.json").is_file(),
        "auth_providers": provider_names,
        "active_provider": auth.get("active_provider"),
        "oauth": oauth,
        "config_exists": (home / "config.yaml").is_file(),
        "model": model,
        "config_provider": provider,
    }


def inspect_shared_nous(root: Path) -> dict[str, Any] | None:
    """Inspect <root>/shared/nous_auth.json — the install-wide Nous OAuth slot.

    Honors HERMES_SHARED_AUTH_DIR, the same override auth.py reads.
    """
    override = os.environ.get("HERMES_SHARED_AUTH_DIR", "").strip()
    base = Path(override).expanduser() if override else root / "shared"
    path = base / "nous_auth.json"
    if not path.is_file():
        return None
    state = read_json(path)
    if not state:
        return None
    summary = identity_of(state)
    summary["path"] = str(path)
    summary["updated_at"] = state.get("updated_at")
    summary["overridden"] = bool(override)
    return summary


def verdict(p: dict[str, Any], *, multiplex: bool, global_auth: dict[str, Any],
            served: bool, expected_account: str | None = None
            ) -> tuple[list[str], list[str]]:
    """Return (blocking problems, non-blocking notes) for one profile."""
    problems: list[str] = []
    notes: list[str] = []
    has_oauth = bool(p.get("oauth"))

    if not p["valid_id"]:
        problems.append(
            "directory name is not a valid profile id "
            "([a-z0-9][a-z0-9_-]{0,63}) — the gateway skips it entirely"
        )
    if not served:
        problems.append(
            "not in gateway.multiplex_profile_allowlist — the gateway never serves it"
        )

    has_env_cred = any(v["set"] for v in p["credential_keys"].values())
    has_generic_key = any(
        p["credential_keys"].get(k, {}).get("set") for k in GENERIC_INFERENCE_KEYS
    )
    global_providers = global_auth.get("providers")
    global_provider_names = (
        set(global_providers.keys()) if isinstance(global_providers, dict) else set()
    )

    # Per-provider auth.json entries fall back to the global root store
    # (auth.py::_load_provider_state). `active_provider` does NOT.
    global_pool = global_auth.get("credential_pool")
    global_pool_names = (
        set(global_pool.keys()) if isinstance(global_pool, dict) else set()
    )
    # A provider is reachable from this profile via its own auth.json (either
    # the providers{} singleton or a credential_pool entry) or, for a named
    # profile, via the root store's per-provider fallback.
    reachable_providers = set(p["auth_providers"]) | set(p.get("oauth") or {})
    if not p["is_default"]:
        reachable_providers |= global_provider_names | global_pool_names

    cfg_provider = (p["config_provider"] or "").strip().lower()
    if cfg_provider:
        if not has_env_cred and cfg_provider not in reachable_providers:
            problems.append(
                f"config.yaml pins model.provider={cfg_provider!r} but this profile "
                f"has no credential for it (.env has no key, auth.json has no "
                f"{cfg_provider!r} entry, no global fallback entry either)"
            )
    else:
        if not has_generic_key and not p["active_provider"] and not has_env_cred:
            problems.append(
                "no model.provider in config.yaml, no OPENAI_API_KEY/"
                "OPENROUTER_API_KEY in .env, and no active_provider in auth.json "
                "— provider resolution has nothing to select"
            )
        elif not p["active_provider"] and not has_generic_key and has_env_cred:
            problems.append(
                "provider-specific keys exist but neither model.provider nor "
                "active_provider is set — resolution order may pick the wrong one"
            )

    if multiplex and not p["is_default"] and not has_env_cred:
        # auth.json (OAuth) is NOT gated by the secret scope, so a profile
        # authenticated via OAuth still resolves a model without any .env key.
        # Platform bot tokens have no such fallback — they are .env only.
        if has_oauth:
            notes.append(
                "no credential key in .env — fine for OAuth inference, but under "
                "gateway.multiplex_profiles any platform bot token "
                "(TELEGRAM_BOT_TOKEN, DISCORD_BOT_TOKEN, ...) must live in THIS "
                "profile's .env; the root .env and shell exports are not visible"
            )
        elif not p["env_exists"]:
            problems.append(
                "no .env and no OAuth credential: under gateway.multiplex_profiles "
                "the secret scope is authoritative, so this profile sees no "
                "credentials at all"
            )
        else:
            problems.append(
                "`.env` defines no credential key and auth.json holds no OAuth "
                "credential. Under gateway.multiplex_profiles the secret scope is "
                "authoritative — the shell environment and the root ~/.hermes/.env "
                "are NOT visible to this profile"
            )

    if p["env_exists"] and p["env_mode"] not in (None, "0600"):
        problems.append(f".env mode is {p['env_mode']} (expected 0600)")

    for provider, creds in (p.get("oauth") or {}).items():
        for cred in creds:
            if cred.get("expired") and not cred.get("has_refresh_token"):
                problems.append(
                    f"{provider} OAuth access token is expired and the entry has "
                    f"no refresh token — re-login required"
                )
            if cred.get("last_status") in ("dead", "DEAD"):
                problems.append(
                    f"{provider} OAuth credential is marked DEAD"
                    + (f" ({cred['last_error_code']})" if cred.get("last_error_code") else "")
                )
            remaining = cred.get("cooldown_remaining_s")
            if cred.get("last_status") in ("exhausted", "EXHAUSTED"):
                if isinstance(remaining, int) and remaining > 0:
                    problems.append(
                        f"{provider} credential is benched as OUT OF QUOTA for "
                        f"another {remaining // 60}m {remaining % 60}s. Hermes will "
                        f"refuse to use it until then even if the quota has already "
                        f"refilled — the reset time came from the server response, "
                        f"not from a live check. Clear it with: "
                        f"hermes -p {p['name']} auth reset {provider}"
                    )
                else:
                    notes.append(
                        f"{provider} credential was benched as out of quota, but the "
                        f"cooldown has expired — it re-enters rotation on the next call"
                    )
            if expected_account:
                who = describe_identity(cred)
                if who.startswith("account unknown"):
                    notes.append(
                        f"{provider} token carries no identity claim, so the expected "
                        f"account {expected_account!r} cannot be verified from disk"
                    )
                elif expected_account.lower() not in who.lower():
                    problems.append(
                        f"WRONG ACCOUNT: you expect {provider} here to be "
                        f"{expected_account!r}, but the stored token belongs to "
                        f"{who!r}. Any quota or limit Hermes reports for this profile "
                        f"is {who}'s, not {expected_account}'s."
                    )
            if cred.get("last_error_code") in (
                "invalid_grant", "invalid_token", "refresh_token_reused",
            ):
                problems.append(
                    f"{provider} OAuth credential last failed with "
                    f"{cred['last_error_code']} — its refresh token was consumed "
                    f"elsewhere (single-use rotation)"
                )

    return problems, notes


def oauth_collisions(report: list[dict[str, Any]],
                     shared: dict[str, Any] | None) -> list[str]:
    """Flag profiles that ended up bound to the same OAuth account/token.

    Two profiles carrying the same refresh-token fingerprint are racing on a
    single-use token: whichever refreshes first invalidates the other. Two
    profiles resolving to the same account identity means the per-profile
    account separation the user configured has already collapsed.
    """
    findings: list[str] = []
    by_refresh: dict[tuple[str, str], list[str]] = {}
    by_identity: dict[tuple[str, str], list[str]] = {}

    for p in report:
        for provider, creds in (p.get("oauth") or {}).items():
            for cred in creds:
                if cred.get("refresh_fp"):
                    by_refresh.setdefault((provider, cred["refresh_fp"]), []).append(p["name"])
                who = describe_identity(cred)
                if not who.startswith("account unknown"):
                    by_identity.setdefault((provider, who), []).append(p["name"])

    for (provider, fp), names in sorted(by_refresh.items()):
        unique = sorted(set(names))
        if len(unique) > 1:
            findings.append(
                f"{provider}: profiles {', '.join(unique)} hold the SAME refresh "
                f"token (fp {fp}). Refresh tokens are single-use — the first "
                f"profile to refresh invalidates it for the others."
            )

    for (provider, who), names in sorted(by_identity.items()):
        unique = sorted(set(names))
        if len(unique) > 1:
            findings.append(
                f"{provider}: profiles {', '.join(unique)} all resolve to account "
                f"{who!r}. Per-profile account separation has collapsed."
            )

    if shared and shared.get("refresh_fp"):
        owners = sorted({
            p["name"]
            for p in report
            for creds in (p.get("oauth") or {}).values()
            for cred in creds
            if cred.get("refresh_fp") == shared["refresh_fp"]
        })
        others = sorted({
            p["name"] for p in report if "nous" in (p.get("oauth") or {})
        })
        if owners:
            findings.append(
                f"shared/nous_auth.json currently holds the token of: "
                f"{', '.join(owners)} (account {describe_identity(shared)}). Every "
                f"other Nous profile merges this token over its own on the next "
                f"refresh (auth.py::_merge_shared_nous_oauth_state)."
            )
        elif others:
            findings.append(
                f"shared/nous_auth.json holds a token ({describe_identity(shared)}) "
                f"that matches NO profile's stored credential — it will be merged "
                f"into whichever Nous profile refreshes next, overwriting its account."
            )
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=os.environ.get("HERMES_HOME") or "~/.hermes",
                    help="Hermes root (default: $HERMES_HOME or ~/.hermes)")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    ap.add_argument("--expect", action="append", default=[], metavar="PROFILE=ACCOUNT",
                    help="Assert which account a profile should be bound to, e.g. "
                         "--expect coder3=someone@example.com (repeatable). "
                         "Matched against the access token's email/sub claim.")
    args = ap.parse_args()

    expected: dict[str, str] = {}
    for item in args.expect:
        name, sep, account = item.partition("=")
        if not sep or not name.strip() or not account.strip():
            print(f"error: --expect needs PROFILE=ACCOUNT, got {item!r}", file=sys.stderr)
            return 2
        expected[name.strip().lower()] = account.strip()

    root = Path(args.root).expanduser()
    # A HERMES_HOME pointing at a profile means the real root is two levels up.
    if root.parent.name == "profiles":
        root = root.parent.parent
    if not root.is_dir():
        print(f"error: Hermes root not found: {root}", file=sys.stderr)
        return 2

    active_file = root / "active_profile"
    active = active_file.read_text(encoding="utf-8").strip() if active_file.is_file() else "default"

    default_cfg = read_yaml(root / "config.yaml")
    gw = gateway_flags(default_cfg)
    global_auth = read_json(root / "auth.json")
    shared_nous = inspect_shared_nous(root)

    entries: list[tuple[str, Path, bool]] = [("default", root, True)]
    profiles_root = root / "profiles"
    if profiles_root.is_dir():
        for child in sorted(profiles_root.iterdir()):
            if child.is_dir() and child.name != "default":
                entries.append((child.name, child, False))

    allowlist = gw["multiplex_profile_allowlist"]
    report = []
    for name, home, is_default in entries:
        info = inspect_profile(name, home, is_default=is_default)
        served = is_default or allowlist is None or name in allowlist
        info["served_by_gateway"] = served
        info["expected_account"] = expected.get(name.lower())
        info["problems"], info["notes"] = verdict(
            info, multiplex=gw["multiplex_profiles"],
            global_auth=global_auth, served=served,
            expected_account=info["expected_account"])
        report.append(info)

    collisions = oauth_collisions(report, shared_nous)

    if args.json:
        print(json.dumps({"root": str(root), "active_profile": active,
                          "gateway": gw, "shared_nous_store": shared_nous,
                          "oauth_collisions": collisions, "profiles": report},
                         indent=2, ensure_ascii=False))
        return 0 if not (collisions or any(p["problems"] for p in report)) else 1

    print(f"\nHermes root        : {root}")
    print(f"Active profile     : {active}")
    print(f"multiplex_profiles : {gw['multiplex_profiles']}"
          + (f"  (env override: {gw['env_override']})" if gw["env_override"] else ""))
    print(f"profile allowlist  : {allowlist if allowlist is not None else '(none — all profiles served)'}")
    print(f"profile_routes     : {len(gw['profile_routes'])} route(s)")
    if shared_nous:
        state = "EXPIRED" if shared_nous.get("expired") else "valid"
        print(f"shared Nous store  : {shared_nous['path']}")
        print(f"                     account={describe_identity(shared_nous)} "
              f"refresh_fp={shared_nous['refresh_fp']} access={state} "
              f"updated={shared_nous.get('updated_at') or 'unknown'}")
        if shared_nous.get("overridden"):
            print("                     (HERMES_SHARED_AUTH_DIR override in effect)")
    else:
        print("shared Nous store  : absent")

    for p in report:
        label = p["name"] + (f"  [{p['display_name']}]" if p["display_name"] else "")
        print(f"\n{'=' * 68}\n{label}\n  path            : {p['path']}")
        print(f"  .env            : "
              + ("missing" if not p["env_exists"]
                 else f"{p['env_key_count']} key(s), mode {p['env_mode']}"))
        if p["credential_keys"]:
            for key, meta in p["credential_keys"].items():
                state = f"set (fp {meta['fp']})" if meta["set"] else "EMPTY VALUE"
                print(f"      - {key:<32} {state}")
        elif p["env_exists"]:
            print("      (no credential keys — placeholder .env)")
        print(f"  auth.json       : "
              + ("missing" if not p["auth_exists"]
                 else f"providers={p['auth_providers'] or '[]'}, "
                      f"active_provider={p['active_provider'] or 'unset'}"))
        for provider, creds in sorted((p.get("oauth") or {}).items()):
            for cred in creds:
                state = "EXPIRED" if cred.get("expired") else "valid"
                print(f"  oauth[{provider}]".ljust(18) + ": "
                      f"account={describe_identity(cred)}")
                print(f"      slot={cred['slot']} access={state} "
                      f"access_fp={cred['access_fp']} "
                      f"refresh_fp={cred['refresh_fp'] or 'none'}")
                if cred.get("entitlement"):
                    fields = " ".join(f"{k}={v}" for k, v in cred["entitlement"].items())
                    print(f"      entitlement: {fields}")
                if cred.get("last_error_code") or cred.get("last_status"):
                    print(f"      last_error={cred.get('last_error_code')} "
                          f"status={cred.get('last_status')} "
                          f"reason={cred.get('last_error_reason')}")
                remaining = cred.get("cooldown_remaining_s")
                if isinstance(remaining, int):
                    if remaining > 0:
                        print(f"      quota cooldown: {remaining // 60}m "
                              f"{remaining % 60}s remaining (server-supplied)")
                    else:
                        print(f"      quota cooldown: expired "
                              f"{abs(remaining) // 60}m ago")
                if cred.get("last_error_message"):
                    print(f"      server said: {cred['last_error_message'][:160]}")
        print(f"  config.yaml     : "
              + ("missing" if not p["config_exists"]
                 else f"model={p['model'] or 'unset'}, "
                      f"provider={p['config_provider'] or 'unset'}"))
        print(f"  served by gw    : {p['served_by_gateway']}")
        if p.get("expected_account"):
            print(f"  expected account: {p['expected_account']}")
        if p["problems"]:
            print("  VERDICT         : will not work")
            for problem in p["problems"]:
                print(f"      ! {problem}")
        else:
            print("  VERDICT         : account wiring looks complete")
        for note in p.get("notes") or []:
            print(f"      - note: {note}")

    broken = [p["name"] for p in report if p["problems"]]
    print(f"\n{'=' * 68}")
    if collisions:
        print("OAuth account collisions:")
        for finding in collisions:
            print(f"  ! {finding}")
        print()
    if broken:
        print(f"{len(broken)} profile(s) with problems: {', '.join(broken)}")
        if any("`.env`" in problem or "no .env" in problem
               for p in report for problem in p["problems"]):
            print("\nFor the .env findings — copy the working profile's credentials:")
            print("    cp ~/.hermes/profiles/<working>/.env ~/.hermes/profiles/<broken>/.env")
            print("    chmod 600 ~/.hermes/profiles/<broken>/.env")
            print("  or recreate the profile with its credentials cloned:")
            print("    hermes profile create <name> --clone-from <working>")
    elif not collisions:
        print("All profiles have complete account wiring.")

    if collisions:
        print("\nPer-profile OAuth accounts on one install are not isolated by")
        print("default. To keep them apart, give each profile its own shared store:")
        print("    HERMES_SHARED_AUTH_DIR=~/.hermes/profiles/<name>/shared \\")
        print("        hermes -p <name> auth login <provider>")
        print("and export the same value for that profile's gateway/CLI processes.")
        print("Then re-login each affected profile so it gets its own token chain.")
    return 1 if (broken or collisions) else 0


if __name__ == "__main__":
    raise SystemExit(main())
