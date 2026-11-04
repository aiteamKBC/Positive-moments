"""
The marked `kbc-api-https` rewrite block of the Positive Moments OpenLiteSpeed vhost, written by
deploy/harden_api_https.sh (`apply`). HTTPS is never touched. On the plain-HTTP listener (port 80):

  * /api/publisher/...              403 from OpenLiteSpeed: a Bearer token sent over HTTP is already
                                    exposed, and following a redirect would send it again;
  * /api/docs..., /api/schema...    GET/HEAD: 301 to the same path on https://. Any other method: 403.
                                    The staff sign-in form is never served, and a submitted password
                                    never processed, over HTTP; Django is not reached;
  * any other page (not /api/, not the ACME HTTP-01 path), GET/HEAD: 301 to https://.

    python3 deploy/kbc_api_https_rules.py <vhost config file> <domain>    # writes the block in place

Idempotent: an existing marked block is replaced, never duplicated. Refuses (exit message) to switch on
a disabled vhost rewrite block that already holds rules of its own.
"""
from __future__ import annotations

import re
import sys

BEGIN, END = "# BEGIN kbc-api-https (deploy/harden_api_https.sh)", "# END kbc-api-https"


def block(domain: str) -> str:
    return "\n".join([
        BEGIN,
        "RewriteCond %{SERVER_PORT} ^80$",
        "RewriteRule ^/?api/publisher/ - [F,L]",
        "RewriteCond %{SERVER_PORT} ^80$",
        "RewriteCond %{REQUEST_METHOD} ^(GET|HEAD)$",
        f"RewriteRule ^/?api/(docs|schema)(/.*)?$ https://{domain}/api/$1$2 [R=301,L]",
        "RewriteCond %{SERVER_PORT} ^80$",
        "RewriteRule ^/?api/(docs|schema)(/|$) - [F,L]",
        "RewriteCond %{SERVER_PORT} ^80$",
        "RewriteCond %{REQUEST_URI} !^/api/",
        "RewriteCond %{REQUEST_URI} !^/\\.well-known/acme-challenge/",
        "RewriteCond %{REQUEST_METHOD} ^(GET|HEAD)$",
        f"RewriteRule ^/?(.*)$ https://{domain}/$1 [R=301,L]",
        END])


def install(text: str, domain: str) -> str:
    """The vhost config with the block as the first rules of its TOP-LEVEL rewrite block."""
    text = re.sub(rf"\n?{re.escape(BEGIN)}.*?{re.escape(END)}\n?", "\n", text, flags=re.S)   # idempotent
    rules = block(domain)
    lines = text.split("\n")
    depth, start, end = 0, None, None
    for i, line in enumerate(lines):               # the top-level rewrite block only (not one inside a context)
        if depth == 0 and start is None and re.match(r"^\s*rewrite\s*\{", line):
            start = i
        depth += line.count("{") - line.count("}")
        if start is not None and end is None and i > start and depth == 0:
            end = i
            break
    if start is None:
        lines += ["rewrite  {", "  enable                  1", "  rules                   <<<END_rules", rules,
                  "  END_rules", "}", ""]
    else:
        body = lines[start:end + 1]
        has_rules = any(re.match(r"^\s*rules\s+<<<END_rules", line) for line in body)
        enabled = any(re.match(r"^\s*enable\s+1\b", line) for line in body)
        if not enabled:
            if has_rules:
                sys.exit("ABORT: the vhost rewrite block is disabled but holds rules; enabling it would switch them on")
            body = [line for line in body if not re.match(r"^\s*enable\s+\d", line)]
            body.insert(1, "  enable                  1")
        if has_rules:
            j = next(i for i, line in enumerate(body) if re.match(r"^\s*rules\s+<<<END_rules", line))
            body.insert(j + 1, rules)
        else:
            body[-1:-1] = ["  rules                   <<<END_rules", rules, "  END_rules"]
        lines[start:end + 1] = body
    return "\n".join(lines)


def main(argv: list[str]) -> None:
    path, domain = argv[1], argv[2]
    with open(path, encoding="utf-8") as handle:
        text = install(handle.read(), domain)           # a refusal exits here, before the file is opened to write
    with open(path, "w", encoding="utf-8") as handle:    # in place: owner and mode kept
        handle.write(text)
    print("rules block written")


if __name__ == "__main__":
    main(sys.argv)
