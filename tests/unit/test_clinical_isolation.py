"""Automated test: Clinical engine has zero imports from financial modules.

Build Manifest item 20: "An automated test exists that scans the clinical
engine's code and fails if any prohibited connection to financial data is found."

This test inspects the actual source code of clinical_engine.py and its direct
dependencies (clinical_nlp.py, clinical_guidelines_ingester.py) and asserts
that none of them import any module containing financial data.
"""

import inspect
import pytest


# Modules that form the clinical engine (F1)
CLINICAL_MODULES = [
    "app.services.clinical_engine",
    "app.services.clinical_nlp",
    "app.services.clinical_guidelines_ingester",
]

# Modules that contain financial data — the clinical engine MUST NOT import these
FINANCIAL_MODULES = [
    "app.services.benchmark",
    "app.services.pricing_engine",
    "app.services.price_discovery",
    "app.services.cost_prediction",
    "app.services.stop_loss",
    "app.services.payment",
    "app.services.employer_dashboard",
    "app.services.broker_advisory",
    "app.services.provider_offers",
    "app.models.price_data",
    "app.models.price_comparison",
    "app.models.employer",  # contains baseline_cost_pepm
    "app.models.provider_offer",
]


def _get_source(module_path: str) -> str:
    """Import a module and return its source code."""
    mod = __import__(module_path, fromlist=[""])
    return inspect.getsource(mod)


@pytest.mark.parametrize("clinical_module", CLINICAL_MODULES)
def test_no_financial_imports(clinical_module):
    """Each clinical module's source code must not import any financial module."""
    source = _get_source(clinical_module)

    violations = []
    for financial_mod in FINANCIAL_MODULES:
        short_name = financial_mod.split(".")[-1]
        # Check for direct import statements in source text
        if f"import {short_name}" in source or f"from {financial_mod}" in source:
            violations.append(financial_mod)

    assert violations == [], (
        f"ISOLATION VIOLATION in {clinical_module}: "
        f"Found imports of financial modules: {violations}. "
        f"The clinical engine must have zero logical pathway to financial data."
    )


def test_clinical_engine_no_price_data_access():
    """clinical_engine.py must never reference PriceData, PriceComparison, or Employer."""
    source = _get_source("app.services.clinical_engine")

    for forbidden in ["PriceData", "PriceComparison", "baseline_cost_pepm", "Employer"]:
        assert forbidden not in source, (
            f"ISOLATION VIOLATION: clinical_engine.py references '{forbidden}'. "
            f"The clinical engine must not access financial data models."
        )


def test_clinical_engine_no_env_vars():
    """clinical_engine.py must not read financial env vars."""
    source = _get_source("app.services.clinical_engine")

    financial_env_vars = [
        "STRIPE_SECRET_KEY",
        "PRICING_API_KEY",
        "BILLING_API_KEY",
        "PAYMENT_GATEWAY_URL",
    ]

    for var in financial_env_vars:
        assert var not in source, (
            f"ISOLATION VIOLATION: clinical_engine.py references env var '{var}'."
        )
