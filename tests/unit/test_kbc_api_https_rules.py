"""
The OpenLiteSpeed HTTPS rules (deploy/kbc_api_https_rules.py, applied by deploy/harden_api_https.sh):
over plain HTTP the staff API documentation is never served and never accepts a form - GET/HEAD are
redirected to https://, everything else is refused - while every answer the block gave before
(LMS API refused, pages redirected, other /api/ routes and ACME untouched, HTTPS untouched) is kept.
Evaluated with tests/ols_rewrite.py; the vhost file handling is checked on sample configs.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tests.ols_rewrite import evaluate

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "kbc_api_https_rules.py"
_spec = importlib.util.spec_from_file_location("kbc_api_https_rules", DEPLOY)
rules_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rules_module)

DOMAIN = "positive-moments.kentbusinesscollege.net"
RULES = rules_module.block(DOMAIN)
# The block as deployed before the API documentation (commit 6f74dee and earlier).
PREVIOUS = "\n".join([
    "RewriteCond %{SERVER_PORT} ^80$",
    "RewriteRule ^/?api/publisher/ - [F,L]",
    "RewriteCond %{SERVER_PORT} ^80$",
    "RewriteCond %{REQUEST_URI} !^/api/",
    "RewriteCond %{REQUEST_URI} !^/\\.well-known/acme-challenge/",
    "RewriteCond %{REQUEST_METHOD} ^(GET|HEAD)$",
    f"RewriteRule ^/?(.*)$ https://{DOMAIN}/$1 [R=301,L]",
])
DOCS = ["/api/docs/", "/api/docs", "/api/docs/login/", "/api/docs/login/?next=/api/docs/", "/api/docs/logout/",
        "/api/schema/", "/api/schema"]
OTHERS = ["/api/publisher/v1/health", "/api/publisher/v1/occurrences/OCC-EXAMPLE-0001/recording",
          "/api/publisher/v1/occurrences?limit=5", "/", "/login?next=x", "/publishing", "/api/auth/sso/exchange/",
          "/api/operations/day/", "/api/health/", "/api/docsearch/", "/api/schemas/", "/.well-known/acme-challenge/x"]


def http(method, uri, rules=RULES):
    return evaluate(rules, port=80, method=method, uri=uri)


@pytest.mark.parametrize("uri", DOCS)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_plain_http_docs_reads_are_redirected_to_the_same_https_url(method, uri):
    assert http(method, uri) == ("301", f"https://{DOMAIN}{uri}")


@pytest.mark.parametrize("uri", DOCS)
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_plain_http_docs_submissions_are_refused_by_the_web_server(method, uri):
    assert http(method, uri) == ("403", None)


@pytest.mark.parametrize("uri", DOCS + OTHERS)
@pytest.mark.parametrize("method", ["GET", "HEAD", "POST"])
def test_https_is_never_touched(method, uri):
    assert evaluate(RULES, port=443, method=method, uri=uri) == ("pass", None)


@pytest.mark.parametrize("uri", OTHERS)
@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT"])
def test_every_other_plain_http_answer_is_what_it_was(method, uri):
    assert http(method, uri) == http(method, uri, PREVIOUS)


def test_the_previous_guarantees_still_hold():
    assert http("GET", "/api/publisher/v1/health") == ("403", None)
    assert http("POST", "/api/publisher/v1/health") == ("403", None)
    assert http("GET", "/login?next=x") == ("301", f"https://{DOMAIN}/login?next=x")
    assert http("POST", "/api/auth/sso/exchange/") == ("pass", None)
    assert http("GET", "/.well-known/acme-challenge/x") == ("pass", None)


# ------------------------------------------------------------------ writing the block into the vhost config
VHCONF = """docRoot                   $VH_ROOT/html/
rewrite  {
  enable                  1
  rules                   <<<END_rules
RewriteRule ^/?legacy$ /new [R=302,L]
  END_rules
}
context /api/ {
  rewrite  {
    enable 1
  }
}
"""


def test_the_block_is_written_first_and_rewriting_is_idempotent():
    once = rules_module.install(VHCONF, DOMAIN)
    lines = once.split("\n")
    assert lines[lines.index("  rules                   <<<END_rules") + 1] == rules_module.BEGIN
    assert "RewriteRule ^/?legacy$ /new [R=302,L]" in once and "context /api/ {" in once
    assert rules_module.install(once, DOMAIN) == once


def test_an_older_block_is_replaced_not_duplicated():
    older = rules_module.install(VHCONF, DOMAIN).replace(rules_module.block(DOMAIN),
                                                         "\n".join([rules_module.BEGIN, PREVIOUS, rules_module.END]))
    upgraded = rules_module.install(older, DOMAIN)
    assert upgraded.count(rules_module.BEGIN) == 1 and RULES in upgraded and upgraded == rules_module.install(VHCONF, DOMAIN)


def test_a_vhost_without_a_rewrite_block_gets_one():
    written = rules_module.install("docRoot $VH_ROOT/html/\n", DOMAIN)
    assert "rewrite  {" in written and "  enable                  1" in written and RULES in written


def test_a_disabled_block_with_its_own_rules_is_refused_and_the_file_is_left_alone(tmp_path):
    disabled = VHCONF.replace("  enable                  1", "  enable                  0", 1)
    target = tmp_path / "vhconf.conf"
    target.write_text(disabled, encoding="utf-8")
    with pytest.raises(SystemExit):
        rules_module.main(["kbc_api_https_rules.py", str(target), DOMAIN])
    assert target.read_text(encoding="utf-8") == disabled
