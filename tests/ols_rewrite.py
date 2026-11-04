"""
A minimal mod_rewrite evaluator for the OpenLiteSpeed vhost rules of deploy/kbc_api_https_rules.py.

It understands exactly what those rules use: RewriteCond on %{SERVER_PORT}, %{REQUEST_URI} and
%{REQUEST_METHOD} (a leading ! negates), RewriteRule with "-" or a target using $1..$9, the flags F,
R=<code> and L, and Apache's query-string rule (kept on a redirect unless the target has its own).
Conditions apply to the next RewriteRule only. The answer for one request:

    ("403", None)          refused by the web server (F)
    ("301", location)      redirected (R=301)
    ("pass", None)         handed on to the backend (Django or the console)
"""
from __future__ import annotations

import re


def evaluate(rules: str, *, port: int, method: str, uri: str) -> tuple[str, str | None]:
    path, _, query = uri.partition("?")
    env = {"SERVER_PORT": str(port), "REQUEST_URI": path, "REQUEST_METHOD": method}
    conditions: list[tuple[str, str]] = []
    for line in rules.splitlines():
        parts = line.split()
        if not parts or parts[0].startswith("#"):
            continue
        if parts[0] == "RewriteCond":
            conditions.append((parts[1], parts[2]))
            continue
        if parts[0] != "RewriteRule":
            raise ValueError(f"not understood: {line}")
        pattern, target = parts[1], parts[2]
        flags = parts[3].strip("[]").split(",") if len(parts) > 3 else []
        pending, conditions = conditions, []
        match = re.search(pattern, path)
        if not match or not all(_holds(variable, test, env) for variable, test in pending):
            continue
        if "F" in flags:
            return "403", None
        redirect = next((flag for flag in flags if flag.startswith("R")), None)
        if redirect:
            location = re.sub(r"\$(\d)", lambda m: match.group(int(m.group(1))) or "", target)
            if query and "?" not in location:
                location += "?" + query
            return redirect.partition("=")[2] or "302", location
        if "L" in flags:
            return "pass", None
    return "pass", None


def _holds(variable: str, test: str, env: dict) -> bool:
    value = env[re.fullmatch(r"%\{(\w+)\}", variable).group(1)]
    negated = test.startswith("!")
    return (re.search(test[1:] if negated else test, value) is not None) != negated
