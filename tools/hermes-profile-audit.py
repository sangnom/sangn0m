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
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
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
        "config_exists": (home / "config.yaml").is_file(),
        "model": model,
        "config_provider": provider,
    }


def verdict(p: dict[str, Any], *, multiplex: bool, global_auth: dict[str, Any],
            served: bool) -> list[str]:
    """Return the reasons this profile cannot resolve an account, if any."""
    problems: list[str] = []

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
    reachable_providers = set(p["auth_providers"]) | (
        set() if p["is_default"] else global_provider_names
    )

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

    if multiplex and not p["is_default"]:
        if not p["env_exists"]:
            problems.append(
                "no .env: under gateway.multiplex_profiles the secret scope is "
                "authoritative, so this profile sees no credentials at all"
            )
        elif not has_env_cred:
            problems.append(
                "`.env` exists but defines no credential key. Under "
                "gateway.multiplex_profiles the secret scope is authoritative — "
                "the shell environment and the root ~/.hermes/.env are NOT visible "
                "to this profile"
            )

    if p["env_exists"] and p["env_mode"] not in (None, "0600"):
        problems.append(f".env mode is {p['env_mode']} (expected 0600)")

    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=os.environ.get("HERMES_HOME") or "~/.hermes",
                    help="Hermes root (default: $HERMES_HOME or ~/.hermes)")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = ap.parse_args()

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
        info["problems"] = verdict(info, multiplex=gw["multiplex_profiles"],
                                   global_auth=global_auth, served=served)
        report.append(info)

    if args.json:
        print(json.dumps({"root": str(root), "active_profile": active,
                          "gateway": gw, "profiles": report}, indent=2,
                         ensure_ascii=False))
        return 0

    print(f"\nHermes root        : {root}")
    print(f"Active profile     : {active}")
    print(f"multiplex_profiles : {gw['multiplex_profiles']}"
          + (f"  (env override: {gw['env_override']})" if gw["env_override"] else ""))
    print(f"profile allowlist  : {allowlist if allowlist is not None else '(none — all profiles served)'}")
    print(f"profile_routes     : {len(gw['profile_routes'])} route(s)")

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
        print(f"  config.yaml     : "
              + ("missing" if not p["config_exists"]
                 else f"model={p['model'] or 'unset'}, "
                      f"provider={p['config_provider'] or 'unset'}"))
        print(f"  served by gw    : {p['served_by_gateway']}")
        if p["problems"]:
            print("  VERDICT         : will not work")
            for problem in p["problems"]:
                print(f"      ! {problem}")
        else:
            print("  VERDICT         : account wiring looks complete")

    broken = [p["name"] for p in report if p["problems"]]
    print(f"\n{'=' * 68}")
    if broken:
        print(f"{len(broken)} profile(s) with problems: {', '.join(broken)}")
        print("\nMost common fix — copy the working profile's credentials:")
        print("    cp ~/.hermes/profiles/<working>/.env ~/.hermes/profiles/<broken>/.env")
        print("    chmod 600 ~/.hermes/profiles/<broken>/.env")
        print("  or recreate the profile with its credentials cloned:")
        print("    hermes profile create <name> --clone-from <working>")
    else:
        print("All profiles have complete account wiring.")
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
